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
