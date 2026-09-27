#import <Foundation/Foundation.h>

@protocol RelayCoreRPC
- (void)exchange:(NSData *)request reply:(void (^)(NSData *))reply;
@end
