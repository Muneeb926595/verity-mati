/* Verity shim: Winsock 2 (declarations only; analysed as network I/O) */
#ifndef VERITY_WIN32_SHIM_WINSOCK2_H
#define VERITY_WIN32_SHIM_WINSOCK2_H
#include "windows.h"
typedef UINT_PTR SOCKET;
#define INVALID_SOCKET ((SOCKET)(~0))
#define SOCKET_ERROR (-1)
#define AF_INET 2
#define SOCK_STREAM 1
#define SOCK_DGRAM 2
#define IPPROTO_TCP 6
#define IPPROTO_UDP 17
struct in_addr { unsigned long s_addr; };
struct sockaddr { unsigned short sa_family; char sa_data[14]; };
struct sockaddr_in { short sin_family; unsigned short sin_port; struct in_addr sin_addr; char sin_zero[8]; };
typedef struct WSAData { WORD wVersion; WORD wHighVersion; char szDescription[257]; char szSystemStatus[129]; } WSADATA, *LPWSADATA;
int WINAPI WSAStartup(WORD wVersionRequested, LPWSADATA lpWSAData);
int WINAPI WSACleanup(void);
int WINAPI WSAGetLastError(void);
SOCKET WINAPI socket(int af, int type, int protocol);
int WINAPI bind(SOCKET s, const struct sockaddr *name, int namelen);
int WINAPI listen(SOCKET s, int backlog);
SOCKET WINAPI accept(SOCKET s, struct sockaddr *addr, int *addrlen);
int WINAPI connect(SOCKET s, const struct sockaddr *name, int namelen);
int WINAPI send(SOCKET s, const char *buf, int len, int flags);
int WINAPI recv(SOCKET s, char *buf, int len, int flags);
int WINAPI sendto(SOCKET s, const char *buf, int len, int flags, const struct sockaddr *to, int tolen);
int WINAPI recvfrom(SOCKET s, char *buf, int len, int flags, struct sockaddr *from, int *fromlen);
int WINAPI closesocket(SOCKET s);
unsigned short WINAPI (htons)(unsigned short hostshort);
unsigned long WINAPI (htonl)(unsigned long hostlong);
unsigned short WINAPI (ntohs)(unsigned short netshort);
unsigned long WINAPI inet_addr(const char *cp);
#endif
