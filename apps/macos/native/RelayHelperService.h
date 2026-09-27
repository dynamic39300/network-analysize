#import <Foundation/Foundation.h>
#import "RelayMutation.h"

@interface RelayHelperService : NSObject <NSXPCListenerDelegate>
- (instancetype)initWithExecutor:(RelayMutationExecutor *)executor identity:(NSDictionary *)identity;
- (void)startWithListener:(NSXPCListener *)listener requirement:(NSString *)requirement;
- (void)close;
@end
