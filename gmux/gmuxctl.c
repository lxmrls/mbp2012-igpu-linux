/*
 * gmuxctl — minimal userspace control of the Apple gmux display multiplexer
 * (indexed protocol, gmux v3.x as on MacBookPro10,1). Mirrors the register
 * handling in Linux drivers/platform/x86/apple-gmux.c.
 *
 *   gmuxctl status        print version, switch state, dGPU power
 *   gmuxctl igd|dis       route panel (display + DDC/AUX + external) to Intel / NVIDIA
 *   gmuxctl ddc igd|dis   move only the DDC/AUX line (harmless test of the write path)
 *
 * Root only (needs iopl). Refuses to write unless the gmux reports major version 3.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/io.h>

#define BASE                   0x700
#define P_VERSION_MAJOR        0x04
#define P_SWITCH_DISPLAY       0x10
#define P_SWITCH_GET_DISPLAY   0x11
#define P_SWITCH_DDC           0x28
#define P_SWITCH_EXTERNAL      0x40
#define P_SWITCH_GET_EXTERNAL  0x41
#define P_DISCRETE_POWER       0x50
#define P_VALUE                0xc2
#define P_READ                 0xd0
#define P_WRITE                0xd4

static int wait_ready(void)
{
    int i = 200;
    unsigned char gwr = inb(BASE + P_WRITE);
    while (i && (gwr & 1)) {
        inb(BASE + P_READ);
        gwr = inb(BASE + P_WRITE);
        usleep(100);
        i--;
    }
    return !!i;
}

static int wait_complete(void)
{
    int i = 200;
    unsigned char gwr = inb(BASE + P_WRITE);
    while (i && !(gwr & 1)) {
        gwr = inb(BASE + P_WRITE);
        usleep(100);
        i--;
    }
    if (gwr & 1)
        inb(BASE + P_READ);
    return !!i;
}

static unsigned char read8(int port)
{
    wait_ready();
    outb(port & 0xff, BASE + P_READ);
    wait_complete();
    return inb(BASE + P_VALUE);
}

static unsigned int read32(int port)
{
    wait_ready();
    outb(port & 0xff, BASE + P_READ);
    wait_complete();
    return inl(BASE + P_VALUE);
}

static void write8(int port, unsigned char val)
{
    outb(val, BASE + P_VALUE);
    wait_ready();
    outb(port & 0xff, BASE + P_WRITE);
    wait_complete();
}

static const char *disp_name(unsigned v) { return v == 2 ? "IGD" : v == 3 ? "DIS" : "?"; }
static const char *ddc_name(unsigned v)  { return v == 1 ? "IGD" : v == 2 ? "DIS" : "?"; }

static int status(void)
{
    unsigned int ver = read32(P_VERSION_MAJOR);
    unsigned d = read8(P_SWITCH_DISPLAY), gd = read8(P_SWITCH_GET_DISPLAY);
    unsigned c = read8(P_SWITCH_DDC);
    unsigned e = read8(P_SWITCH_EXTERNAL), ge = read8(P_SWITCH_GET_EXTERNAL);
    unsigned p = read8(P_DISCRETE_POWER);
    printf("version=%u.%u.%u\n", ver >> 24, (ver >> 16) & 0xff, (ver >> 8) & 0xff);
    printf("display=%s (0x%02x, get 0x%02x)\n", disp_name(d), d, gd);
    printf("ddc=%s (0x%02x)\n", ddc_name(c), c);
    printf("external=%s (0x%02x, get 0x%02x)\n", disp_name(e), e, ge);
    printf("dgpu_power=%s (0x%02x)\n", p == 3 ? "on" : p == 0 ? "off" : "?", p);
    return 0;
}

static int check_version(void)
{
    unsigned int ver = read32(P_VERSION_MAJOR);
    if ((ver >> 24) != 3) {
        fprintf(stderr, "gmuxctl: unexpected gmux version %u.%u.%u, refusing to write\n",
                ver >> 24, (ver >> 16) & 0xff, (ver >> 8) & 0xff);
        return 0;
    }
    return 1;
}

int main(int argc, char **argv)
{
    if (argc < 2) {
        fprintf(stderr, "usage: gmuxctl status | igd | dis | ddc igd|dis\n");
        return 2;
    }
    if (iopl(3) < 0) {
        perror("gmuxctl: iopl (need root)");
        return 1;
    }
    if (!strcmp(argv[1], "status"))
        return status();
    if (!check_version())
        return 1;
    if (!strcmp(argv[1], "ddc") && argc == 3) {
        int igd = !strcmp(argv[2], "igd");
        write8(P_SWITCH_DDC, igd ? 1 : 2);
        unsigned c = read8(P_SWITCH_DDC);
        printf("ddc=%s (0x%02x)\n", ddc_name(c), c);
        return c == (igd ? 1u : 2u) ? 0 : 1;
    }
    if (!strcmp(argv[1], "igd") || !strcmp(argv[1], "dis")) {
        int igd = !strcmp(argv[1], "igd");
        /* same order as gmux_write_switch_state(): DDC, display, external */
        write8(P_SWITCH_DDC, igd ? 1 : 2);
        write8(P_SWITCH_DISPLAY, igd ? 2 : 3);
        write8(P_SWITCH_EXTERNAL, igd ? 2 : 3);
        unsigned d = read8(P_SWITCH_DISPLAY), c = read8(P_SWITCH_DDC);
        printf("display=%s ddc=%s\n", disp_name(d), ddc_name(c));
        return (d == (igd ? 2u : 3u) && c == (igd ? 1u : 2u)) ? 0 : 1;
    }
    fprintf(stderr, "gmuxctl: unknown command\n");
    return 2;
}
