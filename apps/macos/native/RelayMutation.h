#import <Foundation/Foundation.h>

// Implementations hold a configuration lock from readTarget until unlock.
@protocol RelayPreferencesAccess
- (NSDictionary *)readTarget:(NSDictionary *)target field:(NSString *)field;
- (NSDictionary *)current;
- (BOOL)commit:(NSDictionary *)snapshot;
- (void)unlock;
@end

@interface RelaySystemPreferences : NSObject <RelayPreferencesAccess>
+ (NSDictionary *)targetNamed:(NSString *)name interface:(NSString *)interface;
@end

@interface RelayMutationExecutor : NSObject
+ (BOOL)validRequest:(NSDictionary *)request;
- (instancetype)initWithDirectory:(NSString *)directory preferences:(id<RelayPreferencesAccess>)preferences;
- (NSDictionary *)perform:(NSDictionary *)request user:(uid_t)user session:(uint32_t)session;
- (NSDictionary *)control;
- (void)saveControl:(NSDictionary *)value;
- (NSDictionary *)batchHistory:(NSString *)identifier;
- (void)saveBatchHistory:(NSDictionary *)value identifier:(NSString *)identifier;
- (void)requireBatchCapacityForRecovery:(BOOL)recovery;
- (void)requireFreshControl;
- (void)requireUnusedOperations:(NSArray *)requests;
- (NSDictionary *)inspect:(NSDictionary *)request user:(uid_t)user session:(uint32_t)session;
- (NSDictionary *)settle:(NSDictionary *)request user:(uid_t)user session:(uint32_t)session restored:(BOOL)restored;
- (NSDictionary *)reconcile:(NSDictionary *)request user:(uid_t)user session:(uint32_t)session restored:(BOOL)restored;
@end
