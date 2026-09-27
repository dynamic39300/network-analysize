#import <Foundation/Foundation.h>
#import "RelaySignature.h"
#import "RelayHelperService.h"
#import "RelayFileSecurity.h"
#include <fcntl.h>
#include <signal.h>
#include <sys/stat.h>
#include <unistd.h>

static BOOL RootDirectory(void) {
    int parent = open("/", O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (parent < 0) return NO;
    struct stat root;
    if (fstat(parent, &root) || root.st_uid != 0 || (root.st_mode & 022) || !RelayNoAllowACL(parent)) { close(parent); return NO; }
    NSArray *components = @[@"Library", @"Application Support", @"com.wangxinlei.relay", @"privileged"];
    for (NSUInteger index = 0; index < components.count; index++) {
        const char *name = [components[index] fileSystemRepresentation];
        if (index >= 2 && mkdirat(parent, name, 0700) && errno != EEXIST) { close(parent); return NO; }
        int child = openat(parent, name, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
        close(parent);
        if (child < 0) return NO;
        struct stat info;
        if (fstat(child, &info) || info.st_uid != 0 || (info.st_mode & (index >= 2 ? 077 : 022)) || !RelayNoAllowACL(child)) { close(child); return NO; }
        parent = child;
    }
    close(parent);
    return YES;
}

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        if (argc == 2 && !strcmp(argv[1], "--verify-pair")) {
            BOOL trusted = RelayTrustedPair().count != 0;
            fputs(trusted ? "{\"trusted_pair\":true}\n" : "{\"trusted_pair\":false}\n", stdout);
            return trusted ? 0 : 1;
        }
        if (argc == 2 && (!strcmp(argv[1], "--identity") || !strcmp(argv[1], "--self-check"))) {
            NSData *data = [NSJSONSerialization dataWithJSONObject:RelayRunningIdentity() options:0 error:NULL];
            if (!data) return 1;
            fwrite(data.bytes, 1, data.length, stdout); fputc('\n', stdout);
            return 0;
        }
        if (argc != 1 || geteuid() != 0) { fputs("RelayHelper requires its approved system service.\n", stderr); return 77; }
        if (@available(macOS 13.0, *)) {
            NSDictionary *pair = RelayTrustedPair();
            if (pair.count == 0) { fputs("RelayHelper signed build pair is invalid.\n", stderr); return 77; }
            umask(0077);
            if (!RootDirectory()) { fputs("RelayHelper private directory is unavailable.\n", stderr); return 73; }
            @try {
                RelayMutationExecutor *executor = [[RelayMutationExecutor alloc] initWithDirectory:
                    @"/Library/Application Support/com.wangxinlei.relay/privileged" preferences:[RelaySystemPreferences new]];
                RelayHelperService *service = [[RelayHelperService alloc] initWithExecutor:executor identity:
                    @{ @"app_hash": pair[@"app_hash"], @"helper_hash": pair[@"helper_hash"], @"team": pair[@"team"] }];
                NSXPCListener *listener = [[NSXPCListener alloc] initWithMachServiceName:@"com.wangxinlei.relay.helper"];
                [service startWithListener:listener requirement:pair[@"app_requirement"]];
                signal(SIGTERM, SIG_IGN); signal(SIGINT, SIG_IGN);
                NSMutableArray *signals = [NSMutableArray array];
                for (NSNumber *number in @[@(SIGTERM), @(SIGINT)]) {
                    dispatch_source_t source = dispatch_source_create(DISPATCH_SOURCE_TYPE_SIGNAL, number.unsignedIntValue, 0,
                        dispatch_get_global_queue(QOS_CLASS_UTILITY, 0));
                    dispatch_source_set_event_handler(source, ^{ [service close]; exit(0); });
                    [signals addObject:source]; dispatch_resume(source);
                }
                dispatch_main();
            } @catch (NSException *exception) {
                (void)exception; fputs("RelayHelper state is unavailable; no new work accepted.\n", stderr); return 74;
            }
        }
        return 77;
    }
}
