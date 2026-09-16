/* SPDX-License-Identifier: GPL-3.0-only */
#define main p3_helper_main
#include "../custom_components/aqara_p3/native/p3lan.c"
#undef main

int main(int argc,char **argv) {
 if(argc!=2)return 2;
 int fd=open(argv[1],O_RDONLY); if(fd<0)return 3;
 struct energy_layout layout;
 int ok=energy_symbols(fd,&layout); close(fd);
 if(!ok)return 2;
 printf("%u",layout.origin);
 for(int i=0;i<ENERGY_SYMBOLS;i++)printf(" %u",layout.value[i]);
 putchar('\n'); return 0;
}
