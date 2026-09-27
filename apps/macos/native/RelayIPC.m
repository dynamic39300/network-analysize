#import <Foundation/Foundation.h>
#import "RelayMutation.h"
#import "RelaySignature.h"
#import "RelayRPC.h"
#include <bsm/libbsm.h>

// NSXPC requires clang's extended protocol signatures; a Python-created
// Objective-C protocol cannot safely describe its decoded argument classes.
@interface RelayNativeIPC : NSObject
+ (NSXPCInterface *)interface;
+ (NSDictionary *)identity;
+ (NSNumber *)sessionIdentifier;
+ (NSDictionary *)targetNamed:(NSString *)name interface:(NSString *)interface;
+ (NSDictionary *)trustedPair;
@end

@implementation RelayNativeIPC
+ (NSXPCInterface *)interface {
    return [NSXPCInterface interfaceWithProtocol:@protocol(RelayCoreRPC)];
}
+ (NSDictionary *)targetNamed:(NSString *)name interface:(NSString *)interface {
    return [RelaySystemPreferences targetNamed:name interface:interface];
}

+ (NSNumber *)sessionIdentifier {
    auditinfo_addr_t info = {0};
    if (getaudit_addr(&info, sizeof(info)) != 0) {
        return nil;
    }
    return @(info.ai_asid);
}

+ (NSDictionary *)identity {
    return RelayRunningIdentity();
}
+ (NSDictionary *)trustedPair { return RelayTrustedPair(); }
@end
