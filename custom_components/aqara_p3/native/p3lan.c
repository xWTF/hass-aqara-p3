/* Aqara P3 temporary IPC/capture adapter. SPDX-License-Identifier: GPL-3.0-only
 * Copyright (c) 2026 Aqara P3 contributors
 * Build with Zig 0.14.1: zig cc -target mipsel-linux.3.10-musleabi -mcpu=mips32r2
 * -msoft-float -Os -static -s p3lan.c -o p3lan-helper
 * No daemon, cloud, configuration edits, raw UART opens, or downloads.
 */
#define _GNU_SOURCE
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/file.h>
#include <sys/ptrace.h>
#include <sys/wait.h>
#include <sys/syscall.h>
#include <sys/prctl.h>
#include <sys/stat.h>
#include <sys/mount.h>
#include <fcntl.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <time.h>
#include <dirent.h>
#include <elf.h>

enum { ENERGY_READY, ENERGY_MATCH, ENERGY_BATCHES, ENERGY_SAMPLES, ENERGY_PENDING, ENERGY_SYMBOLS };
struct energy_layout { uint32_t value[ENERGY_SYMBOLS], origin; };
/* A member of the vendor's heap-allocated match-info structure, not an ELF
 * symbol. No type information for this structure is shipped. Keep this ABI
 * offset restricted to the firmware fingerprints verified by the caller. */
enum { MATCH_INFO_COMMITTED_WH = 0x1b8 };

static int elf_span(size_t size,uint32_t offset,uint64_t count) {
 return offset<=size && count<=size-offset;
}
static const char *elf_string(const unsigned char *data,const Elf32_Shdr *strings,uint32_t offset) {
 if(offset>=strings->sh_size)return NULL;
 const char *s=(const char *)data+strings->sh_offset+offset;
 return memchr(s,0,strings->sh_size-offset) ? s : NULL;
}
static int compiler_local(const char *name,const char *prefix) {
 size_t n=strlen(prefix);
 return !strncmp(name,prefix,n) && name[n] && strspn(name+n,"0123456789")==strlen(name+n);
}
/* Read .symtab, not dlsym(): these are LOCAL symbols in another process.
 * FILE scope, OBJECT size and uniqueness distinguish compiler-generated names.
 * No address fallback is allowed if the table is absent or ambiguous. */
static int energy_symbols(int fd,struct energy_layout *layout) {
 struct stat st; Elf32_Ehdr eh; unsigned char *data=NULL; int ok=0,origins=0,tables=0;
 unsigned found[ENERGY_SYMBOLS]={0};
 memset(layout,0,sizeof(*layout));
 if(fstat(fd,&st) || !S_ISREG(st.st_mode) || st.st_size<(off_t)sizeof(eh) || st.st_size>2097152)return 0;
 size_t size=(size_t)st.st_size;
 data=malloc(size); if(!data)return 0;
 if(pread(fd,data,size,0)!=(ssize_t)size)goto finish;
 memcpy(&eh,data,sizeof(eh));
 if(memcmp(eh.e_ident,ELFMAG,SELFMAG) || eh.e_ident[EI_CLASS]!=ELFCLASS32 ||
    eh.e_ident[EI_DATA]!=ELFDATA2LSB || eh.e_ident[EI_VERSION]!=EV_CURRENT ||
    eh.e_type!=ET_DYN || eh.e_machine!=EM_MIPS || eh.e_version!=EV_CURRENT ||
    eh.e_ehsize!=sizeof(eh) || eh.e_shentsize!=sizeof(Elf32_Shdr) ||
    !eh.e_shnum || eh.e_shnum>512 || !elf_span(size,eh.e_shoff,(uint64_t)eh.e_shnum*sizeof(Elf32_Shdr)) ||
    eh.e_phentsize!=sizeof(Elf32_Phdr) || !eh.e_phnum || eh.e_phnum>128 ||
    !elf_span(size,eh.e_phoff,(uint64_t)eh.e_phnum*sizeof(Elf32_Phdr)))goto finish;
 for(unsigned i=0;i<eh.e_phnum;i++) {
  Elf32_Phdr ph; memcpy(&ph,data+eh.e_phoff+i*sizeof(ph),sizeof(ph));
  if(ph.p_type==PT_LOAD && !ph.p_offset && (ph.p_flags&PF_X) && ph.p_filesz>=sizeof(eh)) {
   if(ph.p_filesz>ph.p_memsz || !elf_span(size,ph.p_offset,ph.p_filesz))goto finish;
   layout->origin=ph.p_vaddr; origins++;
  }
 }
 if(origins!=1)goto finish;
 for(unsigned i=0;i<eh.e_shnum;i++) {
  Elf32_Shdr symtab,strings; memcpy(&symtab,data+eh.e_shoff+i*sizeof(symtab),sizeof(symtab));
  if(symtab.sh_type!=SHT_SYMTAB)continue;
  if(++tables!=1 || symtab.sh_entsize!=sizeof(Elf32_Sym) || symtab.sh_size%sizeof(Elf32_Sym) ||
     symtab.sh_size/sizeof(Elf32_Sym)>65536 || symtab.sh_link>=eh.e_shnum ||
     !elf_span(size,symtab.sh_offset,symtab.sh_size))goto finish;
  memcpy(&strings,data+eh.e_shoff+symtab.sh_link*sizeof(strings),sizeof(strings));
  if(strings.sh_type!=SHT_STRTAB || !elf_span(size,strings.sh_offset,strings.sh_size))goto finish;
  const char *scope="";
  for(unsigned j=0;j<symtab.sh_size/sizeof(Elf32_Sym);j++) {
   Elf32_Sym symbol; memcpy(&symbol,data+symtab.sh_offset+j*sizeof(symbol),sizeof(symbol));
   const char *name=elf_string(data,&strings,symbol.st_name); if(!name)goto finish;
   if(ELF32_ST_TYPE(symbol.st_info)==STT_FILE){scope=name;continue;}
   if(ELF32_ST_TYPE(symbol.st_info)!=STT_OBJECT || ELF32_ST_BIND(symbol.st_info)!=STB_LOCAL)continue;
   int key=-1; uint32_t length=4;
   if(!strcmp(scope,"ha_ir_master.c") && !strcmp(name,"g_match_info"))key=ENERGY_MATCH;
   else if(!strcmp(scope,"ha_ir.c") && !strcmp(name,"__compound_literal.0")){key=ENERGY_READY;length=52;}
   else if(!strcmp(scope,"ha_ir_power.c")) {
    if(compiler_local(name,"time_cout."))key=ENERGY_BATCHES;
    else if(compiler_local(name,"sum_power."))key=ENERGY_PENDING;
    else if(symbol.st_size==4 && compiler_local(name,"count."))key=ENERGY_SAMPLES;
   }
   if(key<0)continue;
   if(symbol.st_size!=length || symbol.st_shndx==SHN_UNDEF || symbol.st_shndx>=eh.e_shnum ||
      symbol.st_value<layout->origin || (uint64_t)symbol.st_value+length>0x80000000ULL || ++found[key]!=1)goto finish;
   Elf32_Shdr section; memcpy(&section,data+eh.e_shoff+symbol.st_shndx*sizeof(section),sizeof(section));
   if((section.sh_flags&(SHF_ALLOC|SHF_WRITE))!=(SHF_ALLOC|SHF_WRITE) || symbol.st_value<section.sh_addr ||
      (uint64_t)symbol.st_value+length>(uint64_t)section.sh_addr+section.sh_size)goto finish;
   layout->value[key]=symbol.st_value;
  }
 }
 ok=tables==1;
 for(int i=0;i<ENERGY_SYMBOLS;i++)if(found[i]!=1)ok=0;
finish:
 free(data); return ok;
}

static volatile sig_atomic_t done;
static void stop(int sig) { (void)sig; done=1; }
static double now(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC,&t); return t.tv_sec+t.tv_nsec/1e9; }
static int number(const char *s,int lo,int hi) {
 char *end; long n=strtol(s,&end,10);
 return *s && !*end && n>=lo && n<=hi ? (int)n : -1;
}
static int ipc(const char *request) {
 /* HA builds validated JSON; allow only the two verified local service roles. */
 size_t size=strlen(request);
 int ir=strstr(request,"\"_to\":512") &&
    (strstr(request,"\"method\":\"miIO.ir_play\"") ||
     strstr(request,"\"method\":\"local.ir_read\"") ||
     strstr(request,"\"method\":\"local.spec_ir\""));
 int properties=strstr(request,"\"_to\":32,") &&
    (strstr(request,"\"method\":\"get_properties\"") ||
     strstr(request,"\"method\":\"set_properties\""));
 if(size>1000 || request[0]!='{' || (!ir && !properties)) return 2;
 int fd=socket(AF_UNIX,SOCK_SEQPACKET,0); if(fd<0) return 3;
 struct timeval timeout={3,0};
 setsockopt(fd,SOL_SOCKET,SO_RCVTIMEO,&timeout,sizeof(timeout));
 setsockopt(fd,SOL_SOCKET,SO_SNDTIMEO,&timeout,sizeof(timeout));
 struct sockaddr_un addr={.sun_family=AF_UNIX}; strcpy(addr.sun_path,"/tmp/miio_agent.socket");
 if(connect(fd,(struct sockaddr *)&addr,sizeof(addr))<0) { close(fd); return 3; }
 const char bind[]="{\"method\":\"bind\",\"address\":8192}";
 if(send(fd,bind,sizeof(bind)-1,MSG_NOSIGNAL)!=(ssize_t)sizeof(bind)-1 ||
    send(fd,request,size,MSG_NOSIGNAL)!=(ssize_t)size) { close(fd); return 4; }
 char buf[8193]; ssize_t n=recv(fd,buf,sizeof(buf)-1,MSG_TRUNC); close(fd);
 if(n<=0 || n>8192) return 5; /* Delivery uncertain: must not retry. */
 buf[n]=0; puts(buf); return 0;
}

static int capture(int pid,int duration) {
#if !defined(__mips__)
 (void)pid; (void)duration; return 2;
#else
 char path[128],value[128];
 snprintf(path,sizeof(path),"/proc/%d/comm",pid);
 FILE *f=fopen(path,"r"); if(!f) return 3;
 int ok=fgets(value,sizeof(value),f)!=NULL && !strcmp(value,"mha_ir\n"); fclose(f);
 if(!ok) return 2;
 int uart=-1;
 for(int i=3;i<32;i++) {
  snprintf(path,sizeof(path),"/proc/%d/fd/%d",pid,i);
  ssize_t n=readlink(path,value,sizeof(value)-1);
  if(n>0) { value[n]=0; if(!strcmp(value,"/dev/ttyS2")) {uart=i;break;} }
 }
 if(uart<0) return 3;
 snprintf(path,sizeof(path),"/proc/%d/mem",pid);
 int mem=open(path,O_RDONLY); if(mem<0)return 3;
 if(ptrace(PTRACE_SEIZE,pid,0,PTRACE_O_TRACESYSGOOD)<0) {close(mem);return 3;}
 int stopped=0,status=0,rc=0,bytes=0;
 uint32_t pending=0,addr=0; double deadline=now()+duration;
 if(ptrace(PTRACE_INTERRUPT,pid,0,0)<0) {rc=3;goto cleanup;}
 if(waitpid(pid,&status,0)!=pid || !WIFSTOPPED(status)) {rc=3;goto cleanup;}
 stopped=1;
 if(ptrace(PTRACE_SYSCALL,pid,0,0)<0) {rc=3;goto cleanup;}
 stopped=0; printf("READY %d\n",getpid()); fflush(stdout);
 while(!done && now()<deadline && bytes<524288) {
  int n=waitpid(pid,&status,WNOHANG);
  if(n<0) {if(errno==EINTR)continue;rc=3;break;}
  if(!n) {usleep(1000);continue;}
  if(!WIFSTOPPED(status)){rc=3;break;} stopped=1;
  int sig=WSTOPSIG(status),deliver=0,count=0; unsigned char data[8192];
  if(sig==(SIGTRAP|0x80)) {
   /* Linux MIPS always returns 38 64-bit registers for GETREGS. */
   uint64_t regs[38];
   if(ptrace(PTRACE_GETREGS,pid,0,regs)<0){rc=3;break;}
   uint32_t v0=regs[2];
   if(pending) {
    if((uint32_t)regs[7]==0 && (int32_t)v0>0 && v0<=sizeof(data)) {
     if(pread(mem,data,v0,addr)!=(ssize_t)v0){rc=3;break;}
     count=v0;
    }
    pending=0;
   } else if(v0==SYS_read && (uint32_t)regs[4]==(uint32_t)uart) {
    pending=1;addr=regs[5];
   }
  } else if(sig!=SIGTRAP) deliver=sig;
  /* Resume before formatting/output to minimize time the device is stopped. */
  if(ptrace(PTRACE_SYSCALL,pid,0,deliver)<0){rc=3;break;} stopped=0;
  if(count) {
   char line[16416]; int pos=snprintf(line,sizeof(line),"RX ");
   static const char hex[]="0123456789abcdef";
   for(int i=0;i<count;i++){line[pos++]=hex[data[i]>>4];line[pos++]=hex[data[i]&15];}
   line[pos++]='\n';
   if(write(STDOUT_FILENO,line,pos)!=pos){done=1;rc=4;}
   bytes+=count;
  }
 }
cleanup:
 if(!stopped) {
  ptrace(PTRACE_INTERRUPT,pid,0,0); double until=now()+2;
  while(now()<until) {
   int n=waitpid(pid,&status,WNOHANG);
   if(n==pid){stopped=WIFSTOPPED(status);break;}
   if(n<0 && errno!=EINTR)break;
   usleep(1000);
  }
 }
 if(stopped && ptrace(PTRACE_DETACH,pid,0,0)<0)rc=3;
 close(mem); printf("DONE %d\n",rc); return rc;
#endif
}

static int mode_mount(int remove) {
 const char *source="/tmp/aqara-p3-local-mode/app_monitor.sh";
 const char *target="/bin/app_monitor.sh";
 struct stat a,b;
 if(lstat(source,&a) || lstat(target,&b) || !S_ISREG(a.st_mode) || !S_ISREG(b.st_mode) || a.st_uid!=geteuid()) return 2;
 int same=a.st_dev==b.st_dev && a.st_ino==b.st_ino;
 if(remove) return same ? (umount(target)<0 ? 3 : 0) : 2;
 if(same)return 0;
 return mount(source,target,NULL,MS_BIND,NULL)<0 ? 3 : 0;
}

/* Resolve variable locations from the verified ELF, then apply ASLR load bias.
 * The caller attests both firmware hashes for the remaining structure ABI.
 * Passive reads only: never attach, stop, call into, or write the IR process. */
static int energy(void) {
 struct energy_layout layout;
 int elf=open("/lib/libha_ir.so",O_RDONLY|O_CLOEXEC); if(elf<0)return 3;
 int resolved=energy_symbols(elf,&layout); close(elf); if(!resolved)return 2;
 DIR *d=opendir("/proc"); if(!d)return 3;
 struct dirent *entry; int pid=0; char path[128],line[512],boot[64];
 while((entry=readdir(d))) {
  int candidate=number(entry->d_name,2,4194304); if(candidate<0)continue;
  snprintf(path,sizeof(path),"/proc/%d/comm",candidate);
  FILE *f=fopen(path,"r"); if(!f)continue;
  int match=fgets(line,sizeof(line),f) && !strcmp(line,"mha_ir\n"); fclose(f);
  if(match) {if(pid){closedir(d);return 3;}pid=candidate;}
 }
 closedir(d); if(!pid)return 3;
 snprintf(path,sizeof(path),"/proc/%d/exe",pid);
 ssize_t n=readlink(path,line,sizeof(line)-1); if(n<0)return 3; line[n]=0;
 if(strcmp(line,"/bin/mha_ir"))return 2;
 snprintf(path,sizeof(path),"/proc/%d/maps",pid);
 FILE *f=fopen(path,"r"); if(!f)return 3;
 unsigned long base=0,start,end,offset; char perms[8],name[256];
 while(fgets(line,sizeof(line),f)) {
  name[0]=0;
  if(sscanf(line,"%lx-%lx %7s %lx %*s %*s %255s",&start,&end,perms,&offset,name)==5 &&
     !offset && !strcmp(name,"/lib/libha_ir.so") && !strcmp(perms,"r-xp"))base=start;
 }
 fclose(f); if(!base || base>0x7ff00000UL || base<layout.origin)return 3;
 base-=layout.origin;
 for(int i=0;i<ENERGY_SYMBOLS;i++)if((uint64_t)base+layout.value[i]+52>0x80000000ULL)return 3;
 f=fopen("/proc/sys/kernel/random/boot_id","r"); if(!f)return 3;
 int ok=fgets(boot,sizeof(boot),f)!=NULL; fclose(f); if(!ok)return 3;
 boot[strcspn(boot,"\r\n")]=0;
 if(strlen(boot)!=36 || strspn(boot,"0123456789abcdef-")!=36)return 3;
 snprintf(path,sizeof(path),"/proc/%d/stat",pid);
 f=fopen(path,"r"); if(!f)return 3;
 ok=fgets(line,sizeof(line),f)!=NULL; fclose(f); if(!ok)return 3;
 char *p=strrchr(line,')'),*save=NULL,*token; unsigned long long ticks=0;
 if(!p)return 3;
 token=strtok_r(p+2," ",&save);
 for(int field=3;token;field++,token=strtok_r(NULL," ",&save)) {
  if(field==22){ticks=strtoull(token,NULL,10);break;}
 }
 if(!ticks)return 3;
 snprintf(path,sizeof(path),"/proc/%d/mem",pid);
 int mem=open(path,O_RDONLY|O_CLOEXEC); if(mem<0)return 3;
 uint32_t pointer=0,first[3],second[3],committed=0,check=0; unsigned char ready=0;
 if(pread(mem,&ready,1,base+layout.value[ENERGY_READY])!=1 || ready!=1 ||
    pread(mem,&pointer,4,base+layout.value[ENERGY_MATCH])!=4 || pointer<0x10000 || pointer>0x7fff0000 || (pointer&3)) {close(mem);return 3;}
 ok=0;
 for(int attempt=0;attempt<5;attempt++) {
  int read_ok=1;
  for(int i=0;i<3;i++)if(pread(mem,&first[i],4,base+layout.value[ENERGY_BATCHES+i])!=4)read_ok=0;
  if(!read_ok || pread(mem,&committed,4,pointer+MATCH_INFO_COMMITTED_WH)!=4)break;
  for(int i=0;i<3;i++)if(pread(mem,&second[i],4,base+layout.value[ENERGY_BATCHES+i])!=4)read_ok=0;
  uint32_t pointer_check;
  if(!read_ok || pread(mem,&check,4,pointer+MATCH_INFO_COMMITTED_WH)!=4 ||
     pread(mem,&pointer_check,4,base+layout.value[ENERGY_MATCH])!=4 || pointer_check!=pointer ||
     pread(mem,&ready,1,base+layout.value[ENERGY_READY])!=1 || ready!=1)break;
  if(!memcmp(first,second,12) && committed==check && first[0]<=2 && first[1]<600 && first[2]<=8000000 && committed<=1000000000U){ok=1;break;}
  usleep(1000);
 }
 close(mem); if(!ok)return 3;
 printf("{\"epoch\":\"%s:%d:%llu\",\"committed_wh\":%u,\"pending_ws\":%u}\n",boot,pid,ticks,committed,first[2]);
 return 0;
}

static int local_mode(const char *script,const char *action,int lock,const char *helper) {
 const char *prefix="/tmp/aqara-p3-local-mode-";
 size_t n=strlen(prefix);
 if(strncmp(script,prefix,n) || strlen(script)!=n+15 || strcmp(script+n+12,".sh")) return 2;
 for(size_t i=n;i<n+12;i++) if(!strchr("0123456789abcdef",script[i])) return 2;
 if(strcmp(action,"status") && strcmp(action,"enable") && strcmp(action,"disable")) return 2;
 struct stat st;
 if(lstat(script,&st) || !S_ISREG(st.st_mode) || st.st_uid!=geteuid()) return 2;
 pid_t child=fork(); if(child<0)return 3;
 if(!child) {
  close(lock); /* Background services must never inherit our lock. */
  execl("/bin/sh","sh",script,action,helper,(char *)NULL);
  _exit(127);
 }
 double deadline=now()+25; int status,ending=0;
 for(;;) {
  pid_t result=waitpid(child,&status,WNOHANG);
  if(result==child)return WIFEXITED(status)?WEXITSTATUS(status):5;
  if(result<0 && errno!=EINTR)return 5;
  if(!ending && (done || now()>deadline)) {
   kill(child,SIGTERM); ending=1; deadline=now()+5;
  } else if(ending && now()>deadline) {
   kill(child,SIGKILL); waitpid(child,&status,0); return 5;
  }
  usleep(10000);
 }
}

int main(int argc,char **argv) {
 if(argc==2 && !strcmp(argv[1],"version")){puts("p3lan-native-1");return 0;}
 /* Internal fixed-path mount calls: parent local-mode already holds flock. */
 if(argc==2 && !strcmp(argv[1],"mode-bind"))return mode_mount(0);
 if(argc==2 && !strcmp(argv[1],"mode-unbind"))return mode_mount(1);
 signal(SIGPIPE,SIG_IGN);
 signal(SIGTERM,stop); signal(SIGHUP,stop); signal(SIGINT,stop);
 prctl(PR_SET_PDEATHSIG,SIGHUP);
 if(argc==2 && !strcmp(argv[1],"energy")){alarm(4);return energy();}
 int lock=open("/tmp/p3lan-native.lock",O_CREAT|O_RDWR,0600);
 if(lock<0 || flock(lock,LOCK_EX|LOCK_NB)<0){
  if(argc>1 && !strcmp(argv[1],"local-mode")) puts("P3_LOCAL_ERROR busy");
  else fputs("BUSY\n",stderr);
  return 6;
 }
 int rc=2;
 if(argc==3 && !strcmp(argv[1],"ipc")){alarm(8);rc=ipc(argv[2]);}
 else if(argc==4 && !strcmp(argv[1],"local-mode")) rc=local_mode(argv[2],argv[3],lock,argv[0]);
 else if(argc==4 && !strcmp(argv[1],"capture")) {
  int pid=number(argv[2],2,4194304),seconds=number(argv[3],1,300);
  if(pid>0 && seconds>0) {alarm(seconds+5);rc=capture(pid,seconds);}
 }
 close(lock); return rc;
}
