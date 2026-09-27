#import "RelayHelperService.h"
#import "RelayRPC.h"

static NSString *Identifier(void) { return [[NSUUID.UUID.UUIDString stringByReplacingOccurrencesOfString:@"-" withString:@""] lowercaseString]; }
static BOOL Keys(id value, NSArray *keys) {
    return [value isKindOfClass:NSDictionary.class] && [[NSSet setWithArray:[value allKeys]] isEqual:[NSSet setWithArray:keys]];
}
static BOOL ID(id value) {
    return [value isKindOfClass:NSString.class] && [value length] == 32 &&
        [value rangeOfCharacterFromSet:[[NSCharacterSet characterSetWithCharactersInString:@"0123456789abcdef"] invertedSet]].location == NSNotFound;
}
static BOOL IsBool(id value) { return value && CFGetTypeID((__bridge CFTypeRef)value) == CFBooleanGetTypeID(); }
static NSData *Encode(NSDictionary *value) {
    return [NSJSONSerialization dataWithJSONObject:value options:0 error:NULL];
}
static BOOL ValidBatch(id requests) {
    if (![requests isKindOfClass:NSArray.class] || [requests count] == 0 || [requests count] > 5) return NO;
    NSMutableSet *identifiers = [NSMutableSet set]; NSString *proposal = nil;
    for (NSDictionary *pair in requests) {
        if (!Keys(pair, @[@"apply", @"restore"]) || ![RelayMutationExecutor validRequest:pair[@"apply"]] ||
            ![RelayMutationExecutor validRequest:pair[@"restore"]]) return NO;
        NSDictionary *apply = pair[@"apply"], *restore = pair[@"restore"];
        if (![apply[@"restore_of"] isEqual:@""]) return NO;
        NSMutableDictionary *reverse = [apply mutableCopy];
        reverse[@"id"] = restore[@"id"]; reverse[@"restore_of"] = apply[@"id"];
        reverse[@"expected"] = apply[@"desired"]; reverse[@"desired"] = apply[@"expected"];
        if (![reverse isEqual:restore] || (proposal && ![proposal isEqual:apply[@"proposal"]])) return NO;
        proposal = apply[@"proposal"];
        [identifiers addObject:apply[@"id"]]; [identifiers addObject:restore[@"id"]];
    }
    return identifiers.count == [requests count] * 2;
}

@interface RelayHelperService ()
@property RelayMutationExecutor *executor;
@property NSCondition *condition;
@property NSMutableSet<NSXPCConnection *> *connections;
@property NSString *instance;
@property NSString *requirement;
@property NSXPCListener *listener;
@property NSMutableDictionary *state;
@property NSLock *operationLock;
@property BOOL stopped;
@property NSUInteger activeCalls;
@property NSTimeInterval batchDeadline;
@property NSDictionary *recoveryReview;
- (NSDictionary *)handle:(NSDictionary *)message connection:(NSXPCConnection *)connection;
@end

@interface RelayHelperPeer : NSObject <RelayCoreRPC>
@property RelayHelperService *service;
@property NSXPCConnection *connection;
@property NSString *nonce;
@property BOOL used;
@property BOOL busy;
@property NSTimeInterval deadline;
@end
@implementation RelayHelperPeer
- (void)exchange:(NSData *)data reply:(void (^)(NSData *))reply {
    RelayHelperService *service = self.service;
    NSXPCConnection *connection = NSXPCConnection.currentConnection;
    [service.condition lock];
    BOOL allowed = !service.stopped && !self.used && !self.busy && connection == self.connection &&
        NSProcessInfo.processInfo.systemUptime < self.deadline && connection.effectiveUserIdentifier >= 501 &&
        connection.auditSessionIdentifier != 0 && (uint32_t)connection.auditSessionIdentifier != UINT32_MAX;
    if (allowed) { service.activeCalls++; self.busy = YES; }
    [service.condition unlock];
    if (!allowed) { [connection invalidate]; return; }
    @try {
        if (![data isKindOfClass:NSData.class] || data.length == 0 || data.length > 32768) { [connection invalidate]; return; }
        NSDictionary *message = [NSJSONSerialization JSONObjectWithData:data options:0 error:NULL];
        if (!self.nonce) {
            if (!Keys(message, @[@"hello"]) || !ID(message[@"hello"])) { [connection invalidate]; return; }
            self.nonce = Identifier();
            reply(Encode(@{@"hello": message[@"hello"], @"nonce": self.nonce, @"version": @1, @"instance": service.instance}));
            return;
        }
        self.used = YES;
        if (!Keys(message, @[@"version", @"instance", @"nonce", @"id", @"client", @"method", @"params"]) ||
            ![message[@"version"] isEqual:@1] || IsBool(message[@"version"]) || ![message[@"instance"] isEqual:service.instance] ||
            ![message[@"nonce"] isEqual:self.nonce] || !ID(message[@"id"]) || !ID(message[@"client"]) ||
            ![message[@"method"] isKindOfClass:NSString.class] || ![message[@"params"] isKindOfClass:NSDictionary.class]) {
            [connection invalidate]; return;
        }
        NSDictionary *result = [service handle:message connection:connection];
        reply(Encode(@{@"instance": service.instance, @"nonce": self.nonce, @"id": message[@"id"], @"result": result}));
    } @catch (NSException *exception) {
        (void)exception;
        [connection invalidate];
    } @finally {
        [service.condition lock]; service.activeCalls--; self.busy = NO; [service.condition broadcast]; [service.condition unlock];
    }
}
@end

@implementation RelayHelperService
- (instancetype)initWithExecutor:(RelayMutationExecutor *)executor identity:(NSDictionary *)identity {
    self = [super init];
    if (!self) return nil;
    self.executor = executor;
    self.condition = [NSCondition new]; self.operationLock = [NSLock new];
    self.connections = [NSMutableSet set]; self.instance = Identifier();
    NSDictionary *saved = [executor control];
    if (!saved) [executor requireFreshControl];
    if (saved && (!Keys(saved, @[@"version", @"identity", @"batch", @"last", @"draining", @"drain_user"]) ||
        ![saved[@"version"] isEqual:@1] || !IsBool(saved[@"draining"]) || ![saved[@"identity"] isEqual:identity] ||
        ([saved[@"draining"] boolValue] && (![saved[@"drain_user"] isKindOfClass:NSNumber.class] ||
            IsBool(saved[@"drain_user"]) || [saved[@"drain_user"] unsignedIntValue] < 501))))
        @throw [NSException exceptionWithName:@"RelayHelperIdentity" reason:@"Stored helper identity changed; trusted upgrade is required" userInfo:nil];
    self.state = saved ? [saved mutableCopy] : [@{@"version": @1, @"identity": identity,
        @"batch": NSNull.null, @"last": NSNull.null, @"draining": @NO, @"drain_user": NSNull.null} mutableCopy];
    [executor saveControl:self.state];
    return self;
}
- (void)startWithListener:(NSXPCListener *)listener requirement:(NSString *)requirement {
    self.listener = listener; self.requirement = requirement;
    listener.delegate = self; [listener resume];
}
- (BOOL)listener:(NSXPCListener *)listener shouldAcceptNewConnection:(NSXPCConnection *)connection {
    (void)listener;
    [self.condition lock];
    if (self.stopped || self.connections.count >= 8 || connection.effectiveUserIdentifier < 501 ||
        connection.auditSessionIdentifier == 0 || (uint32_t)connection.auditSessionIdentifier == UINT32_MAX) {
        [self.condition unlock]; return NO;
    }
    RelayHelperPeer *peer = [RelayHelperPeer new];
    peer.service = self; peer.connection = connection; peer.deadline = NSProcessInfo.processInfo.systemUptime + 5;
    connection.exportedInterface = [NSXPCInterface interfaceWithProtocol:@protocol(RelayCoreRPC)];
    connection.exportedObject = peer;
    if (@available(macOS 13.0, *)) [connection setCodeSigningRequirement:self.requirement];
    else { [self.condition unlock]; return NO; }
    [self.connections addObject:connection];
    __weak RelayHelperService *weakSelf = self;
    __weak NSXPCConnection *weakConnection = connection;
    connection.invalidationHandler = ^{
        RelayHelperService *service = weakSelf;
        NSXPCConnection *invalid = weakConnection;
        [service.condition lock]; if (invalid) [service.connections removeObject:invalid]; [service.condition broadcast]; [service.condition unlock];
        peer.connection = nil;
    };
    connection.interruptionHandler = ^{ [weakConnection invalidate]; };
    [connection resume];
    [self.condition unlock];
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 5 * NSEC_PER_SEC), dispatch_get_global_queue(QOS_CLASS_UTILITY, 0), ^{ [weakConnection invalidate]; });
    return YES;
}
- (BOOL)save:(NSDictionary *)changes {
    NSMutableDictionary *next = [self.state mutableCopy]; [next addEntriesFromDictionary:changes];
    [self.executor saveControl:next]; self.state = next; return YES;
}
- (BOOL)owner:(NSDictionary *)batch message:(NSDictionary *)message connection:(NSXPCConnection *)connection {
    return [batch[@"user"] unsignedIntValue] == connection.effectiveUserIdentifier &&
        [batch[@"session"] unsignedIntValue] == (uint32_t)connection.auditSessionIdentifier &&
        [batch[@"client"] isEqual:message[@"client"]];
}
- (NSArray *)inspectBatch:(NSDictionary *)batch {
    NSMutableArray *values = [NSMutableArray array];
    for (NSDictionary *pair in batch[@"requests"]) [values addObject:[self.executor inspect:pair[@"apply"]
        user:[batch[@"user"] unsignedIntValue] session:[batch[@"session"] unsignedIntValue]]];
    return values;
}
- (BOOL)canRecover:(NSDictionary *)batch message:(NSDictionary *)message connection:(NSXPCConnection *)connection {
    NSDictionary *holder = batch[@"recovery"] ?: batch;
    return [self owner:holder message:message connection:connection] || ![holder[@"instance"] isEqual:self.instance] ||
        [NSDate.date timeIntervalSince1970] >= [holder[@"expires"] doubleValue] ||
        NSProcessInfo.processInfo.systemUptime >= self.batchDeadline;
}
- (NSDictionary *)finishBatch:(NSDictionary *)batch message:(NSDictionary *)message connection:(NSXPCConnection *)connection
    outcome:(NSString *)outcome disposition:(NSString *)disposition recovery:(NSString *)recovery clearActive:(BOOL)clearActive {
    NSDictionary *result = @{@"state": @"finished", @"batch_id": batch[@"batch_id"], @"requests": batch[@"requests"],
        @"user": batch[@"user"], @"session": @(connection.auditSessionIdentifier), @"client": message[@"client"],
        @"outcome": outcome, @"disposition": disposition, @"recovery_id": recovery, @"network_verified": @NO,
        @"completed_at": @([NSDate.date timeIntervalSince1970])};
    [self.executor saveBatchHistory:result identifier:batch[@"batch_id"]];
    if (clearActive) {
        [self save:@{@"batch": NSNull.null, @"last": result}];
        self.recoveryReview = nil;
    }
    return result;
}
- (NSDictionary *)recovery:(NSDictionary *)message connection:(NSXPCConnection *)connection batch:(NSDictionary *)batch {
    NSString *method = message[@"method"]; NSDictionary *params = message[@"params"];
    if (!ID(params[@"batch_id"])) return @{@"error": @"invalid_request"};
    BOOL inspecting = [method isEqual:@"recovery_inspect"];
    BOOL manifest = inspecting && Keys(params, @[@"batch_id", @"requests"]);
    if (manifest && !ValidBatch(params[@"requests"])) return @{@"error": @"invalid_batch"};
    NSDictionary *finished = [self.executor batchHistory:params[@"batch_id"]];
    if (finished) {
        if ([finished[@"user"] unsignedIntValue] != connection.effectiveUserIdentifier) return @{@"error": @"batch_not_found"};
        if (inspecting && (manifest || Keys(params, @[@"batch_id"])))
            return manifest && ![finished[@"requests"] isEqual:params[@"requests"]] ? @{@"error": @"batch_conflict"} : finished;
        if ([method isEqual:@"recovery_end"] && Keys(params, @[@"batch_id", @"recovery_id"]) &&
            [finished[@"recovery_id"] isEqual:params[@"recovery_id"]] &&
            [self owner:finished message:message connection:connection]) return finished;
        return @{@"error": @"batch_finished"};
    }
    if (manifest && (!batch || ![batch[@"batch_id"] isEqual:params[@"batch_id"]])) {
        if ([self.state[@"last"] isKindOfClass:NSDictionary.class] &&
            [self.state[@"last"][@"batch_id"] isEqual:params[@"batch_id"]]) return @{@"error": @"batch_unsettled"};
        NSMutableSet *inUse = [NSMutableSet set];
        for (NSDictionary *pair in batch[@"requests"]) for (NSString *kind in @[@"apply", @"restore"])
            [inUse addObject:pair[kind][@"id"]];
        for (NSDictionary *attempt in batch[@"recovery_attempts"])
            [inUse addObjectsFromArray:attempt[@"restore_ids"]];
        for (NSDictionary *pair in params[@"requests"]) for (NSString *kind in @[@"apply", @"restore"])
            if ([inUse containsObject:pair[kind][@"id"]]) return @{@"error": @"batch_conflict"};
        [self.executor requireBatchCapacityForRecovery:YES];
        [self.executor requireUnusedOperations:params[@"requests"]];
        // Seal the absent ID before reporting no writes: a delayed begin must now be rejected.
        NSDictionary *absent = @{@"batch_id": params[@"batch_id"], @"requests": params[@"requests"],
            @"user": @(connection.effectiveUserIdentifier)};
        return [self finishBatch:absent message:message connection:connection outcome:@"not_started"
            disposition:@"not_started" recovery:@"" clearActive:NO];
    }
    if (!batch || ![batch[@"batch_id"] isEqual:params[@"batch_id"]] ||
        [batch[@"user"] unsignedIntValue] != connection.effectiveUserIdentifier) return @{@"error": @"batch_not_found"};
    if (manifest && ![batch[@"requests"] isEqual:params[@"requests"]]) return @{@"error": @"batch_conflict"};
    if (inspecting && (manifest || Keys(params, @[@"batch_id"]))) {
        NSArray *inspections = [self inspectBatch:batch];
        NSMutableArray *fields = [NSMutableArray array];
        BOOL restore = YES, retain = YES;
        for (NSUInteger index = 0; index < inspections.count; index++) {
            NSDictionary *value = inspections[index], *request = batch[@"requests"][index][@"apply"];
            restore &= [@[@"not_started", @"original", @"desired"] containsObject:value[@"state"]];
            retain &= [value[@"matches_desired"] boolValue];
            [fields addObject:@{@"id": request[@"id"], @"field": request[@"field"], @"target": request[@"target"],
                @"expected": request[@"expected"], @"desired": request[@"desired"], @"state": value[@"state"],
                @"actual_known": value[@"actual"] != nil ? @YES : @NO, @"actual": value[@"actual"] ?: NSNull.null}];
        }
        BOOL eligible = [self canRecover:batch message:message connection:connection];
        NSDictionary *review = @{@"id": Identifier(), @"batch": batch, @"inspections": inspections,
            @"user": @(connection.effectiveUserIdentifier), @"session": @(connection.auditSessionIdentifier), @"client": message[@"client"],
            @"deadline": @(NSProcessInfo.processInfo.systemUptime + 300), @"can_restore": @(restore), @"can_retain": @(retain)};
        self.recoveryReview = review;
        NSDictionary *holder = batch[@"recovery"] ?: batch;
        return @{@"state": @"pending", @"batch_id": batch[@"batch_id"], @"requests": batch[@"requests"], @"fields": fields,
            @"review_id": review[@"id"], @"helper_instance": self.instance, @"eligible": @(eligible),
            @"lease_expires_at": holder[@"expires"], @"can_restore": eligible && restore ? @YES : @NO,
            @"can_retain": eligible && retain ? @YES : @NO};
    }
    if ([method isEqual:@"recovery_apply"] && Keys(params, @[@"batch_id", @"review_id", @"choice"]) &&
        [@[@"restore", @"retain"] containsObject:params[@"choice"]]) {
        NSDictionary *review = self.recoveryReview;
        if (!review || ![review[@"id"] isEqual:params[@"review_id"]] ||
            ![self owner:review message:message connection:connection] ||
            NSProcessInfo.processInfo.systemUptime >= [review[@"deadline"] doubleValue] || ![review[@"batch"] isEqual:batch])
            return @{@"error": @"recovery_review_required"};
        self.recoveryReview = nil;
        BOOL restoring = [params[@"choice"] isEqual:@"restore"];
        if (![self canRecover:batch message:message connection:connection] ||
            ![review[restoring ? @"can_restore" : @"can_retain"] boolValue]) return @{@"error": @"recovery_unavailable"};
        NSArray *inspections = [self inspectBatch:batch];
        if (![inspections isEqual:review[@"inspections"]]) return @{@"error": @"recovery_changed"};
        NSMutableArray *attempts = [batch[@"recovery_attempts"] mutableCopy] ?: [NSMutableArray array];
        BOOL writes = NO;
        if (restoring) for (NSDictionary *value in inspections) writes |= [value[@"state"] isEqual:@"desired"];
        if (writes && attempts.count >= 10) return @{@"error": @"recovery_limit"};
        NSMutableArray *identifiers = [NSMutableArray array];
        for (NSDictionary *pair in batch[@"requests"]) { (void)pair; [identifiers addObject:Identifier()]; }
        NSDictionary *recovery = @{@"id": params[@"review_id"], @"choice": params[@"choice"], @"restore_ids": identifiers,
            @"user": batch[@"user"], @"session": @(connection.auditSessionIdentifier), @"client": message[@"client"],
            @"instance": self.instance, @"expires": @([NSDate.date timeIntervalSince1970] + 300)};
        if (writes) [attempts addObject:recovery];
        NSMutableDictionary *claimed = [batch mutableCopy];
        claimed[@"recovery"] = recovery; claimed[@"recovery_attempts"] = attempts;
        // Persist the new owner and every compensation ID before any configuration write.
        [self save:@{@"batch": claimed}];
        self.batchDeadline = NSProcessInfo.processInfo.systemUptime + 300;
        if (restoring) for (NSUInteger index = 0; index < inspections.count; index++) {
            NSDictionary *value = inspections[index];
            if (![value[@"state"] isEqual:@"desired"]) continue;
            NSMutableDictionary *request = [batch[@"requests"][index][@"restore"] mutableCopy];
            request[@"id"] = identifiers[index];
            NSDictionary *result = [self.executor perform:request user:[batch[@"user"] unsignedIntValue]
                session:[batch[@"session"] unsignedIntValue]];
            if (![result[@"state"] isEqual:@"configured"]) return @{@"state": @"needs_verification", @"recovery_id": recovery[@"id"]};
        }
        return @{@"state": @"ready_to_settle", @"recovery_id": recovery[@"id"]};
    }
    NSDictionary *recovery = batch[@"recovery"];
    if ([method isEqual:@"recovery_end"] && Keys(params, @[@"batch_id", @"recovery_id"]) && recovery &&
        [params[@"recovery_id"] isEqual:recovery[@"id"]] && [recovery[@"instance"] isEqual:self.instance] &&
        [self owner:recovery message:message connection:connection]) {
        BOOL restored = [recovery[@"choice"] isEqual:@"restore"], started = NO;
        for (NSUInteger index = 0; index < [batch[@"requests"] count]; index++) {
            NSDictionary *pair = batch[@"requests"][index];
            NSDictionary *result = [self.executor reconcile:pair[@"apply"] user:[batch[@"user"] unsignedIntValue]
                session:[batch[@"session"] unsignedIntValue] restored:restored];
            if (result[@"error"]) return @{@"error": @"batch_unsettled"};
            started |= ![result[@"state"] isEqual:@"not_started"];
            NSMutableArray *restores = [NSMutableArray arrayWithObject:pair[@"restore"]];
            for (NSDictionary *attempt in batch[@"recovery_attempts"]) {
                NSMutableDictionary *request = [pair[@"restore"] mutableCopy];
                request[@"id"] = attempt[@"restore_ids"][index]; [restores addObject:request];
            }
            for (NSDictionary *request in restores) {
                NSDictionary *settled = [self.executor reconcile:request user:[batch[@"user"] unsignedIntValue]
                    session:[batch[@"session"] unsignedIntValue] restored:!restored];
                if (settled[@"error"]) return @{@"error": @"batch_unsettled"};
            }
        }
        NSString *disposition = !started ? @"not_started" : restored ? @"restored" : @"retained";
        return [self finishBatch:batch message:message connection:connection outcome:@"reconciled"
            disposition:disposition recovery:recovery[@"id"] clearActive:YES];
    }
    return @{@"error": @"recovery_review_required"};
}
- (NSDictionary *)handle:(NSDictionary *)message connection:(NSXPCConnection *)connection {
    if (![self.operationLock tryLock]) return @{@"error": @"helper_busy"};
    @try {
        NSString *method = message[@"method"]; NSDictionary *params = message[@"params"];
        NSDictionary *batch = [self.state[@"batch"] isKindOfClass:NSDictionary.class] ? self.state[@"batch"] : nil;
        if (batch) {
            NSDictionary *finished = [self.executor batchHistory:batch[@"batch_id"]];
            if (finished) {
                if (![finished[@"requests"] isEqual:batch[@"requests"]] || ![finished[@"user"] isEqual:batch[@"user"]])
                    return @{@"error": @"helper_storage_unavailable"};
                // A crash after the terminal receipt only needs metadata completion.
                [self save:@{@"batch": NSNull.null, @"last": finished}]; batch = nil;
            }
        }
        if ([@[@"recovery_inspect", @"recovery_apply", @"recovery_end"] containsObject:method])
            return [self recovery:message connection:connection batch:batch];
        if ([method isEqual:@"status"] && Keys(params, @[])) return @{@"ready": @YES,
            @"draining": self.state[@"draining"], @"batch_active": batch != nil ? @YES : @NO, @"identity": @"signed_build_pair"};
        if (([method isEqual:@"drain"] || [method isEqual:@"resume"]) && Keys(params, @[])) {
            if (batch) return @{@"error": @"batch_unsettled"};
            if ([self.state[@"draining"] boolValue] && [self.state[@"drain_user"] unsignedIntValue] != connection.effectiveUserIdentifier)
                return @{@"error": @"drain_owned_by_another_user"};
            BOOL draining = [method isEqual:@"drain"];
            [self save:@{@"draining": @(draining), @"drain_user": draining ? @(connection.effectiveUserIdentifier) : NSNull.null}];
            return @{@"draining": self.state[@"draining"]};
        }
        if ([method isEqual:@"begin"] && Keys(params, @[@"batch_id", @"requests"]) && ID(params[@"batch_id"])) {
            if ([self.state[@"draining"] boolValue]) return @{@"error": @"helper_draining"};
            NSArray *requests = params[@"requests"];
            if (!ValidBatch(requests)) return @{@"error": @"invalid_batch"};
            if (batch) {
                if (![self owner:batch message:message connection:connection] ||
                    ![batch[@"batch_id"] isEqual:params[@"batch_id"]] || ![batch[@"requests"] isEqual:requests]) return @{@"error": @"batch_unsettled"};
                return @{@"batch_id": batch[@"batch_id"]};
            }
            if ([self.state[@"last"] isKindOfClass:NSDictionary.class] &&
                [self.state[@"last"][@"batch_id"] isEqual:params[@"batch_id"]]) return @{@"error": @"batch_finished"};
            if ([self.executor batchHistory:params[@"batch_id"]]) return @{@"error": @"batch_finished"};
            [self.executor requireBatchCapacityForRecovery:NO];
            NSDictionary *created = @{@"batch_id": params[@"batch_id"], @"requests": requests, @"client": message[@"client"],
                @"user": @(connection.effectiveUserIdentifier), @"session": @(connection.auditSessionIdentifier),
                @"expires": @([NSDate.date timeIntervalSince1970] + 300), @"instance": self.instance};
            [self save:@{@"batch": created}];
            self.batchDeadline = NSProcessInfo.processInfo.systemUptime + 300;
            return @{@"batch_id": created[@"batch_id"]};
        }
        NSDictionary *last = [self.state[@"last"] isKindOfClass:NSDictionary.class] ? self.state[@"last"] : nil;
        if (!batch && last && [method isEqual:@"end"] && Keys(params, @[@"batch_id", @"outcome"]) &&
            [self owner:last message:message connection:connection] && [last[@"batch_id"] isEqual:params[@"batch_id"]] &&
            [last[@"outcome"] isEqual:params[@"outcome"]]) return @{@"settled": @YES};
        if (!batch || ![self owner:batch message:message connection:connection]) return @{@"error": @"batch_required"};
        if (batch[@"recovery"]) return @{@"error": @"recovery_in_progress"};
        if (![batch[@"instance"] isEqual:self.instance]) return @{@"error": @"batch_interrupted"};
        if ([method isEqual:@"perform"] && Keys(params, @[@"request"])) {
            NSDictionary *request = params[@"request"]; BOOL matched = NO, restoring = NO;
            for (NSDictionary *pair in batch[@"requests"]) {
                if ([pair[@"apply"] isEqual:request]) matched = YES;
                if ([pair[@"restore"] isEqual:request]) { matched = YES; restoring = YES; }
            }
            if (!matched) return @{@"error": @"outside_batch"};
            if (!restoring && ([NSDate.date timeIntervalSince1970] >= [batch[@"expires"] doubleValue] ||
                NSProcessInfo.processInfo.systemUptime >= self.batchDeadline)) return @{@"error": @"batch_expired"};
            return [self.executor perform:request user:connection.effectiveUserIdentifier session:connection.auditSessionIdentifier];
        }
        if ([method isEqual:@"end"] && Keys(params, @[@"batch_id", @"outcome"]) &&
            [batch[@"batch_id"] isEqual:params[@"batch_id"]] && [@[@"verified", @"rolled_back", @"blocked"] containsObject:params[@"outcome"]]) {
            BOOL restored = ![params[@"outcome"] isEqual:@"verified"], started = NO;
            for (NSDictionary *pair in batch[@"requests"]) {
                NSDictionary *result = [self.executor settle:pair[@"apply"] user:connection.effectiveUserIdentifier
                    session:connection.auditSessionIdentifier restored:restored];
                if (result[@"error"] || (!restored && ![result[@"state"] isEqual:@"retained"])) return @{@"error": @"batch_unsettled"};
                started |= ![result[@"state"] isEqual:@"not_started"];
                NSDictionary *compensation = [self.executor settle:pair[@"restore"] user:connection.effectiveUserIdentifier
                    session:connection.auditSessionIdentifier restored:!restored];
                if (compensation[@"error"]) return @{@"error": @"batch_unsettled"};
            }
            [self finishBatch:batch message:message connection:connection outcome:params[@"outcome"]
                disposition:!started ? @"not_started" : restored ? @"restored" : @"retained" recovery:@"" clearActive:YES];
            return @{@"settled": @YES};
        }
        return @{@"error": @"invalid_request"};
    } @catch (NSException *exception) { (void)exception; return @{@"error": @"helper_storage_unavailable"}; }
    @finally { [self.operationLock unlock]; }
}
- (void)close {
    [self.condition lock]; self.stopped = YES; NSArray *connections = self.connections.allObjects; [self.condition unlock];
    [self.listener invalidate];
    for (NSXPCConnection *connection in connections) [connection invalidate];
    [self.condition lock]; while (self.activeCalls) [self.condition wait]; [self.condition unlock];
}
@end
