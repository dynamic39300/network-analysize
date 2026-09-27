#import "RelayMutation.h"
#import "RelayFileSecurity.h"
#import <SystemConfiguration/SystemConfiguration.h>
#include <arpa/inet.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <unistd.h>

static void Fail(NSString *reason) {
    @throw [NSException exceptionWithName:@"RelayMutation" reason:reason userInfo:nil];
}
static struct dirent *CheckedEntry(DIR *stream) {
    errno = 0;
    struct dirent *entry = readdir(stream);
    if (!entry && errno) Fail(@"journal_unavailable");
    return entry;
}
static BOOL Keys(NSDictionary *value, NSArray *keys) {
    return [value isKindOfClass:NSDictionary.class] &&
        [[NSSet setWithArray:value.allKeys] isEqual:[NSSet setWithArray:keys]];
}
static BOOL Text(id value, NSUInteger limit) {
    return [value isKindOfClass:NSString.class] && [value length] > 0 && [value length] <= limit &&
        [value rangeOfCharacterFromSet:NSCharacterSet.controlCharacterSet].location == NSNotFound;
}
static BOOL Hex(id value, NSUInteger length) {
    return Text(value, length) && [value length] == length &&
        [value rangeOfCharacterFromSet:[[NSCharacterSet characterSetWithCharactersInString:@"0123456789abcdef"] invertedSet]].location == NSNotFound;
}
static BOOL IsBoolean(id value) {
    return value && CFGetTypeID((__bridge CFTypeRef)value) == CFBooleanGetTypeID();
}
static NSString *ProtocolFor(NSString *field) {
    if ([field isEqual:@"dns"]) return (__bridge NSString *)kSCNetworkProtocolTypeDNS;
    if ([field isEqual:@"ipv6"]) return (__bridge NSString *)kSCNetworkProtocolTypeIPv6;
    if ([@[@"proxy:http", @"proxy:https", @"proxy:socks"] containsObject:field])
        return (__bridge NSString *)kSCNetworkProtocolTypeProxies;
    Fail(@"unsupported_field");
    return nil;
}
static NSString *ProxyPrefix(NSString *field) {
    return @{@"proxy:http": @"HTTP", @"proxy:https": @"HTTPS", @"proxy:socks": @"SOCKS"}[field];
}
static void ValidateValue(NSString *field, id value) {
    if ([field isEqual:@"dns"]) {
        if (![value isKindOfClass:NSArray.class] || [value count] > 16) Fail(@"invalid_value");
        for (id address in value) {
            unsigned char buffer[16];
            if (!Text(address, INET6_ADDRSTRLEN) ||
                [address rangeOfCharacterFromSet:[[NSCharacterSet characterSetWithCharactersInString:@"0123456789abcdefABCDEF:."] invertedSet]].location != NSNotFound ||
                (inet_pton(AF_INET, [address UTF8String], buffer) != 1 &&
                 inet_pton(AF_INET6, [address UTF8String], buffer) != 1)) Fail(@"invalid_address");
        }
    } else if ([field isEqual:@"ipv6"]) {
        if (![@[@"Off", @"Automatic", @"Link-local only"] containsObject:value]) Fail(@"invalid_value");
    } else if (!IsBoolean(value)) Fail(@"invalid_value");
}
static void ValidateRequest(NSDictionary *request) {
    if (!Keys(request, @[@"version", @"id", @"proposal", @"target", @"field", @"expected", @"desired", @"endpoint", @"restore_of"]) ||
        ![request[@"version"] isEqual:@1] || IsBoolean(request[@"version"]) ||
        !Hex(request[@"id"], 32) || !Hex(request[@"proposal"], 64)) Fail(@"invalid_request");
    NSDictionary *target = request[@"target"];
    if (!Keys(target, @[@"service_id", @"name", @"interface"]) || !Text(target[@"service_id"], 128) ||
        !Text(target[@"name"], 256) || !Text(target[@"interface"], 32)) Fail(@"invalid_target");
    NSString *field = request[@"field"];
    if (!Text(field, 32)) Fail(@"unsupported_field");
    ProtocolFor(field);
    ValidateValue(field, request[@"expected"]);
    ValidateValue(field, request[@"desired"]);
    if (![request[@"restore_of"] isEqual:@""] && !Hex(request[@"restore_of"], 32)) Fail(@"invalid_restore");
    if (ProxyPrefix(field)) {
        NSDictionary *endpoint = request[@"endpoint"];
        if (!Keys(endpoint, @[@"host", @"port"]) || !Text(endpoint[@"host"], 255) ||
            ![endpoint[@"port"] isKindOfClass:NSNumber.class] || IsBoolean(endpoint[@"port"]) ||
            [endpoint[@"port"] doubleValue] != [endpoint[@"port"] intValue] ||
            [endpoint[@"port"] intValue] < 1 || [endpoint[@"port"] intValue] > 65535) Fail(@"invalid_endpoint");
    } else if (!Keys(request[@"endpoint"], @[])) Fail(@"invalid_endpoint");
}
static id FieldValue(NSDictionary *snapshot, NSString *field) {
    NSDictionary *config = snapshot[@"configuration"];
    if ([field isEqual:@"dns"]) return config[@"ServerAddresses"] ?: @[];
    if ([field isEqual:@"ipv6"]) {
        if (![snapshot[@"enabled"] boolValue]) return @"Off";
        NSString *method = config[@"ConfigMethod"];
        if ([method isEqual:@"Automatic"]) return @"Automatic";
        if ([method isEqual:@"LinkLocal"]) return @"Link-local only";
        Fail(@"unsupported_ipv6_state");
    }
    NSString *prefix = ProxyPrefix(field);
    id enabled = config[[prefix stringByAppendingString:@"Enable"]] ?: @0;
    if (![enabled isEqual:@0] && ![enabled isEqual:@1]) Fail(@"invalid_proxy_state");
    return @([enabled boolValue]);
}
static void CheckEndpoint(NSDictionary *snapshot, NSDictionary *request) {
    NSString *prefix = ProxyPrefix(request[@"field"]);
    if (!prefix) return;
    NSDictionary *config = snapshot[@"configuration"], *endpoint = request[@"endpoint"];
    if (![config[[prefix stringByAppendingString:@"Proxy"]] isEqual:endpoint[@"host"]] ||
        ![config[[prefix stringByAppendingString:@"Port"]] isEqual:endpoint[@"port"]]) Fail(@"endpoint_changed");
}
static NSDictionary *Changed(NSDictionary *snapshot, NSString *field, id value) {
    NSMutableDictionary *result = [snapshot mutableCopy], *config = [snapshot[@"configuration"] mutableCopy];
    if ([field isEqual:@"dns"]) {
        if ([value count]) config[@"ServerAddresses"] = value;
        else [config removeObjectForKey:@"ServerAddresses"];
    } else if ([field isEqual:@"ipv6"]) {
        result[@"enabled"] = @(![value isEqual:@"Off"]);
        // Disabling preserves the full previous configuration for future restore.
        if (![value isEqual:@"Off"]) config[@"ConfigMethod"] = [value isEqual:@"Automatic"] ? @"Automatic" : @"LinkLocal";
    } else config[[ProxyPrefix(field) stringByAppendingString:@"Enable"]] = @([value boolValue] ? 1 : 0);
    result[@"configuration"] = config;
    return result;
}

@implementation RelaySystemPreferences {
    SCPreferencesRef _preferences;
    SCNetworkProtocolRef _protocol;
    NSDictionary *_target;
    NSString *_field;
}
+ (NSDictionary *)targetNamed:(NSString *)name interface:(NSString *)interface {
    SCPreferencesRef preferences = SCPreferencesCreate(NULL, CFSTR("Relay target identity"), NULL);
    if (!preferences) return nil;
    SCNetworkSetRef set = SCNetworkSetCopyCurrent(preferences);
    NSArray *services = set ? CFBridgingRelease(SCNetworkSetCopyServices(set)) : nil;
    NSMutableArray *matches = [NSMutableArray array];
    for (id item in services) {
        SCNetworkServiceRef service = (__bridge SCNetworkServiceRef)item;
        SCNetworkInterfaceRef link = SCNetworkServiceGetInterface(service);
        if (SCNetworkServiceGetEnabled(service) && link &&
            [(__bridge NSString *)SCNetworkServiceGetName(service) isEqual:name] &&
            [(__bridge NSString *)SCNetworkInterfaceGetBSDName(link) isEqual:interface]) {
            [matches addObject:@{@"service_id": (__bridge NSString *)SCNetworkServiceGetServiceID(service),
                @"name": name, @"interface": interface}];
        }
    }
    if (set) CFRelease(set);
    CFRelease(preferences);
    return matches.count == 1 ? matches[0] : nil;
}
- (NSDictionary *)readTarget:(NSDictionary *)target field:(NSString *)field {
    _target = [target copy];
    _field = [field copy];
    _preferences = SCPreferencesCreate(NULL, CFSTR("Relay privileged mutation"), NULL);
    if (!_preferences || !SCPreferencesLock(_preferences, FALSE)) Fail(@"configuration_busy");
    SCPreferencesSynchronize(_preferences);
    SCNetworkSetRef set = SCNetworkSetCopyCurrent(_preferences);
    NSArray *services = set ? CFBridgingRelease(SCNetworkSetCopyServices(set)) : nil;
    if (set) CFRelease(set);
    for (id item in services) {
        SCNetworkServiceRef service = (__bridge SCNetworkServiceRef)item;
        SCNetworkInterfaceRef link = SCNetworkServiceGetInterface(service);
        if ([(__bridge NSString *)SCNetworkServiceGetServiceID(service) isEqual:target[@"service_id"]] && link &&
            SCNetworkServiceGetEnabled(service) &&
            [(__bridge NSString *)SCNetworkServiceGetName(service) isEqual:target[@"name"]] &&
            [(__bridge NSString *)SCNetworkInterfaceGetBSDName(link) isEqual:target[@"interface"]]) {
            _protocol = SCNetworkServiceCopyProtocol(service, (__bridge CFStringRef)ProtocolFor(field));
            break;
        }
    }
    if (!_protocol) Fail(@"target_changed");
    return [self current];
}
- (NSDictionary *)current {
    // A new reader observes persisted state, not this writer's staged cache.
    SCPreferencesRef reader = SCPreferencesCreate(NULL, CFSTR("Relay mutation readback"), NULL);
    SCNetworkServiceRef service = reader ? SCNetworkServiceCopy(reader, (__bridge CFStringRef)_target[@"service_id"]) : NULL;
    SCNetworkProtocolRef protocol = service ? SCNetworkServiceCopyProtocol(service, (__bridge CFStringRef)ProtocolFor(_field)) : NULL;
    NSDictionary *config = protocol ? (__bridge NSDictionary *)SCNetworkProtocolGetConfiguration(protocol) : nil;
    NSDictionary *result = [config isKindOfClass:NSDictionary.class] ?
        @{@"configuration": [config copy], @"enabled": @(SCNetworkProtocolGetEnabled(protocol))} : nil;
    if (protocol) CFRelease(protocol);
    if (service) CFRelease(service);
    if (reader) CFRelease(reader);
    if (!result) Fail(@"configuration_unavailable");
    return result;
}
- (BOOL)commit:(NSDictionary *)snapshot {
    if (!SCNetworkProtocolSetConfiguration(_protocol, (__bridge CFDictionaryRef)snapshot[@"configuration"]) ||
        !SCNetworkProtocolSetEnabled(_protocol, [snapshot[@"enabled"] boolValue])) return NO;
    if (!SCPreferencesCommitChanges(_preferences)) return NO;
    return SCPreferencesApplyChanges(_preferences);
}
- (void)unlock {
    if (_protocol) { CFRelease(_protocol); _protocol = NULL; }
    if (_preferences) {
        SCPreferencesUnlock(_preferences);
        CFRelease(_preferences);
        _preferences = NULL;
    }
}
- (void)dealloc { [self unlock]; }
@end

@implementation RelayMutationExecutor {
    int _directory;
    int _owner;
    id<RelayPreferencesAccess> _preferences;
    NSLock *_lock;
}
+ (BOOL)validRequest:(NSDictionary *)request {
    @try { ValidateRequest(request); return YES; }
    @catch (NSException *exception) { (void)exception; return NO; }
}
- (instancetype)initWithDirectory:(NSString *)directory preferences:(id<RelayPreferencesAccess>)preferences {
    self = [super init];
    if (!self) return nil;
    _directory = -1;
    _owner = -1;
    _preferences = preferences;
    _lock = [NSLock new];
    _directory = open(directory.fileSystemRepresentation, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    struct stat info;
    if (_directory < 0 || fstat(_directory, &info) || info.st_uid != geteuid() || (info.st_mode & 077) != 0 || !RelayNoAllowACL(_directory)) {
        if (_directory >= 0) close(_directory);
        _directory = -1;
        Fail(@"unsafe_journal_directory");
    }
    _owner = openat(_directory, "owner.lock", O_CREAT | O_RDWR | O_NOFOLLOW | O_CLOEXEC, 0600);
    if (_owner < 0 || fstat(_owner, &info) || !S_ISREG(info.st_mode) || info.st_nlink != 1 ||
        info.st_uid != geteuid() || (info.st_mode & 077) != 0 || !RelayNoAllowACL(_owner) || flock(_owner, LOCK_EX | LOCK_NB)) {
        if (_owner >= 0) close(_owner);
        close(_directory); _owner = _directory = -1;
        Fail(@"executor_owned");
    }
    return self;
}
- (NSString *)nameFor:(NSString *)identifier user:(uid_t)user session:(uint32_t)session {
    return [NSString stringWithFormat:@"%u-%u-%@.json", user, session, identifier];
}
- (void)checkDirectory {
    struct stat info;
    if (fstat(_directory, &info) || info.st_uid != geteuid() || (info.st_mode & 077) || !RelayNoAllowACL(_directory))
        Fail(@"unsafe_journal_directory");
}
- (NSDictionary *)read:(NSString *)name {
    [self checkDirectory];
    int fd = openat(_directory, name.fileSystemRepresentation, O_RDONLY | O_NONBLOCK | O_NOFOLLOW | O_CLOEXEC);
    if (fd < 0 && errno == ENOENT) return nil;
    struct stat info;
    if (fd < 0) Fail(@"journal_unavailable");
    if (fstat(fd, &info) || !S_ISREG(info.st_mode) || info.st_nlink != 1 || info.st_uid != geteuid() ||
        (info.st_mode & 077) != 0 || !RelayNoAllowACL(fd) || info.st_size < 1 || info.st_size > 65536) {
        close(fd); Fail(@"unsafe_journal_record");
    }
    NSMutableData *data = [NSMutableData dataWithLength:(NSUInteger)info.st_size];
    ssize_t count = read(fd, data.mutableBytes, data.length);
    close(fd);
    if (count != (ssize_t)data.length) Fail(@"journal_unavailable");
    id value = [NSJSONSerialization JSONObjectWithData:data options:0 error:NULL];
    if (![value isKindOfClass:NSDictionary.class]) Fail(@"invalid_journal");
    return value;
}
- (void)save:(NSDictionary *)record name:(NSString *)name {
    [self checkDirectory];
    NSData *data = [NSJSONSerialization dataWithJSONObject:record options:NSJSONWritingSortedKeys error:NULL];
    if (!data || data.length > 65536) Fail(@"journal_limit");
    NSString *temporary = [NSUUID.UUID.UUIDString stringByAppendingString:@".tmp"];
    int fd = openat(_directory, temporary.fileSystemRepresentation, O_CREAT | O_EXCL | O_WRONLY | O_NOFOLLOW | O_CLOEXEC, 0600);
    if (fd < 0) Fail(@"journal_unavailable");
    BOOL ok = RelayNoAllowACL(fd) && write(fd, data.bytes, data.length) == (ssize_t)data.length && fsync(fd) == 0;
    close(fd);
    if (ok) ok = renameat(_directory, temporary.fileSystemRepresentation, _directory, name.fileSystemRepresentation) == 0 && fsync(_directory) == 0;
    unlinkat(_directory, temporary.fileSystemRepresentation, 0);
    if (!ok) Fail(@"journal_unavailable");
}
- (NSDictionary *)control { return [self read:@"helper-control"]; }
- (void)saveControl:(NSDictionary *)value { [self save:value name:@"helper-control"]; }
- (NSDictionary *)batchHistory:(NSString *)identifier {
    if (!Hex(identifier, 32)) Fail(@"invalid_batch");
    return [self read:[@"batch-" stringByAppendingString:identifier]];
}
- (void)saveBatchHistory:(NSDictionary *)value identifier:(NSString *)identifier {
    if (!Hex(identifier, 32)) Fail(@"invalid_batch");
    [self save:value name:[@"batch-" stringByAppendingString:identifier]];
}
- (void)requireBatchCapacityForRecovery:(BOOL)recovery {
    [self checkDirectory];
    DIR *stream = fdopendir(dup(_directory));
    if (!stream) Fail(@"journal_unavailable");
    rewinddir(stream);
    NSUInteger count = 0;
    struct dirent *entry;
    @try { while ((entry = CheckedEntry(stream))) if (!strncmp(entry->d_name, "batch-", 6)) count++; }
    @finally { closedir(stream); }
    if (count >= (recovery ? 2000 : 1000)) Fail(@"batch_capacity");
}
- (void)requireFreshControl {
    [self checkDirectory];
    DIR *stream = fdopendir(dup(_directory));
    if (!stream) Fail(@"journal_unavailable");
    rewinddir(stream);
    BOOL fresh = YES;
    struct dirent *entry;
    @try {
        while ((entry = CheckedEntry(stream))) {
            if (strcmp(entry->d_name, ".") && strcmp(entry->d_name, "..") && strcmp(entry->d_name, "owner.lock")) fresh = NO;
        }
    } @finally { closedir(stream); }
    if (!fresh) Fail(@"missing_helper_control");
}
- (void)requireUnusedOperations:(NSArray *)requests {
    [self checkDirectory];
    NSMutableSet *identifiers = [NSMutableSet set];
    for (NSDictionary *pair in requests) for (NSString *kind in @[@"apply", @"restore"])
        [identifiers addObject:pair[kind][@"id"]];
    DIR *stream = fdopendir(dup(_directory));
    if (!stream) Fail(@"journal_unavailable");
    rewinddir(stream);
    @try {
        struct dirent *entry;
        while ((entry = CheckedEntry(stream))) {
            NSString *name = [NSString stringWithUTF8String:entry->d_name];
            NSArray *prior = nil;
            if ([name hasSuffix:@".json"]) {
                NSDictionary *record = [self read:name];
                if (![RelayMutationExecutor validRequest:record[@"request"]]) Fail(@"invalid_journal");
                prior = @[record[@"request"]];
            } else if ([name hasPrefix:@"batch-"]) {
                NSDictionary *record = [self read:name];
                if (![record[@"state"] isEqual:@"finished"] || ![record[@"requests"] isKindOfClass:NSArray.class]) Fail(@"invalid_journal");
                NSMutableArray *operations = [NSMutableArray array];
                for (NSDictionary *pair in record[@"requests"]) {
                    if (!Keys(pair, @[@"apply", @"restore"])) Fail(@"invalid_journal");
                    for (NSString *kind in @[@"apply", @"restore"]) {
                        if (![RelayMutationExecutor validRequest:pair[kind]]) Fail(@"invalid_journal");
                        [operations addObject:pair[kind]];
                    }
                }
                prior = operations;
            }
            for (NSDictionary *request in prior) if ([identifiers containsObject:request[@"id"]]) Fail(@"operation_id_conflict");
        }
    } @finally { closedir(stream); }
}
- (NSDictionary *)inspect:(NSDictionary *)request user:(uid_t)user session:(uint32_t)session {
    if (![_lock tryLock]) return @{@"state": @"unknown"};
    @try {
        ValidateRequest(request);
        NSDictionary *record = [self read:[self nameFor:request[@"id"] user:user session:session]];
        if (!record) return @{@"state": @"not_started"};
        if (![record[@"request"] isEqual:request] ||
            !Keys(record[@"before"], @[@"configuration", @"enabled"]) ||
            ![record[@"before"][@"configuration"] isKindOfClass:NSDictionary.class] ||
            ![FieldValue(record[@"before"], request[@"field"]) isEqual:request[@"expected"]]) Fail(@"invalid_journal");
        CheckEndpoint(record[@"before"], request);
        NSDictionary *current = [_preferences readTarget:request[@"target"] field:request[@"field"]];
        CheckEndpoint(current, request);
        id actual = FieldValue(current, request[@"field"]);
        ValidateValue(request[@"field"], actual);
        BOOL original = [actual isEqual:request[@"expected"]], desired = [actual isEqual:request[@"desired"]];
        // The service retains these full snapshots privately for stale-review checks.
        return @{@"state": original ? @"original" : desired ? @"desired" : @"drift", @"actual": actual,
            @"matches_original": @(original), @"matches_desired": @(desired), @"record": record, @"current": current};
    } @catch (NSException *exception) { (void)exception; return @{@"state": @"unknown"}; }
    @finally { [_preferences unlock]; [_lock unlock]; }
}
- (NSDictionary *)settle:(NSDictionary *)request user:(uid_t)user session:(uint32_t)session restored:(BOOL)restored {
    return [self settle:request user:user session:session restored:restored observed:NO];
}
- (NSDictionary *)reconcile:(NSDictionary *)request user:(uid_t)user session:(uint32_t)session restored:(BOOL)restored {
    return [self settle:request user:user session:session restored:restored observed:YES];
}
- (NSDictionary *)settle:(NSDictionary *)request user:(uid_t)user session:(uint32_t)session restored:(BOOL)restored observed:(BOOL)observed {
    if (![_lock tryLock]) return @{@"error": @"executor_busy"};
    @try {
        ValidateRequest(request);
        NSString *name = [self nameFor:request[@"id"] user:user session:session];
        NSDictionary *record = [self read:name];
        if (!record) return @{@"state": @"not_started"};
        if (![record[@"request"] isEqual:request]) Fail(@"operation_id_conflict");
        NSDictionary *current = [_preferences readTarget:request[@"target"] field:request[@"field"]];
        CheckEndpoint(current, request);
        if (![FieldValue(current, request[@"field"]) isEqual:request[restored ? @"expected" : @"desired"]] ||
            (!restored && !observed && (![record[@"result"][@"state"] isEqual:@"configured"] || record[@"restored_by"])))
            Fail(@"configuration_unconfirmed");
        NSMutableDictionary *settled = [record mutableCopy];
        settled[@"phase"] = @"completed";
        settled[@"disposition"] = restored ? @"restored" : @"retained";
        settled[@"reconciled_at"] = @([NSDate.date timeIntervalSince1970]);
        if (!settled[@"result"]) settled[@"result"] = @{@"error": @"interrupted_then_reconciled", @"id": request[@"id"]};
        [self save:settled name:name];
        return @{@"state": settled[@"disposition"]};
    } @catch (NSException *exception) {
        return @{@"error": [exception.name isEqual:@"RelayMutation"] ? exception.reason : @"operation_failed"};
    } @finally { [_preferences unlock]; [_lock unlock]; }
}
- (void)checkCapacityAndInterrupted:(BOOL)restoring {
    DIR *stream = fdopendir(dup(_directory));
    if (!stream) Fail(@"journal_unavailable");
    rewinddir(stream);
    NSUInteger count = 0;
    BOOL interrupted = NO;
    @try {
        struct dirent *entry;
        while ((entry = readdir(stream))) {
            NSString *name = [NSString stringWithUTF8String:entry->d_name];
            if (![name hasSuffix:@".json"]) continue;
            count++;
            NSDictionary *record = [self read:name];
            if (![record[@"phase"] isEqual:@"completed"]) interrupted = YES;
        }
    } @finally { closedir(stream); }
    // Keep capacity for compensation after ordinary writes have stopped.
    if (count >= (restoring ? 2000 : 1000)) Fail(@"journal_capacity");
    if (interrupted && !restoring) Fail(@"interrupted_operation");
}
- (NSDictionary *)perform:(NSDictionary *)request user:(uid_t)user session:(uint32_t)session {
    if (![_lock tryLock]) return @{@"error": @"executor_busy"};
    @try {
        if (user < 501 || session == 0 || session == UINT32_MAX) Fail(@"invalid_user_session");
        ValidateRequest(request);
        NSString *name = [self nameFor:request[@"id"] user:user session:session];
        NSDictionary *previous = [self read:name];
        if (previous) {
            if (![previous[@"request"] isEqual:request]) Fail(@"operation_id_conflict");
            return previous[@"result"] ?: @{@"error": @"interrupted_operation", @"id": request[@"id"]};
        }
        [self checkCapacityAndInterrupted:[request[@"restore_of"] length] > 0];
        NSDictionary *originalRecord = nil;
        if ([request[@"restore_of"] length]) {
            originalRecord = [self read:[self nameFor:request[@"restore_of"] user:user session:session]];
            NSDictionary *original = originalRecord[@"request"];
            if (!original || ![original[@"restore_of"] isEqual:@""] ||
                ![original[@"proposal"] isEqual:request[@"proposal"]] ||
                ![original[@"target"] isEqual:request[@"target"]] || ![original[@"field"] isEqual:request[@"field"]] ||
                ![original[@"endpoint"] isEqual:request[@"endpoint"]] ||
                ![original[@"desired"] isEqual:request[@"expected"]] ||
                ![original[@"expected"] isEqual:request[@"desired"]]) Fail(@"invalid_restore");
        }
        NSDictionary *before = [_preferences readTarget:request[@"target"] field:request[@"field"]];
        CheckEndpoint(before, request);
        id actual = FieldValue(before, request[@"field"]);
        ValidateValue(request[@"field"], actual);
        if (![actual isEqual:request[@"expected"]]) Fail(@"configuration_changed");
        NSDictionary *desired = Changed(before, request[@"field"], request[@"desired"]);
        if (originalRecord) {
            NSMutableDictionary *restored = [desired mutableCopy], *configuration = [desired[@"configuration"] mutableCopy];
            NSString *key = [request[@"field"] isEqual:@"dns"] ? @"ServerAddresses" :
                [request[@"field"] isEqual:@"ipv6"] ? @"ConfigMethod" : [ProxyPrefix(request[@"field"]) stringByAppendingString:@"Enable"];
            id value = originalRecord[@"before"][@"configuration"][key];
            if (value) configuration[key] = value;
            else [configuration removeObjectForKey:key];
            restored[@"configuration"] = configuration;
            if ([request[@"field"] isEqual:@"ipv6"]) restored[@"enabled"] = originalRecord[@"before"][@"enabled"];
            desired = restored;
        }
        NSMutableDictionary *record = [@{@"version": @1, @"request": request, @"user": @(user), @"session": @(session),
            @"before": before, @"desired": desired, @"phase": @"prepared",
            @"created_at": @([NSDate.date timeIntervalSince1970])} mutableCopy];
        [self save:record name:name];
        BOOL applied = [_preferences commit:desired];
        NSDictionary *readback = [_preferences current];
        BOOL matches = [readback isEqual:desired];
        NSDictionary *result = @{@"id": request[@"id"], @"state": applied && matches ? @"configured" : @"needs_verification",
            @"actual": FieldValue(readback, request[@"field"]), @"applied": @(applied),
            @"network_verified": @NO};
        record[@"phase"] = applied && matches ? @"completed" : @"needs_verification";
        record[@"after"] = readback;
        record[@"result"] = result;
        [self save:record name:name];
        if (originalRecord && applied && matches) {
            NSMutableDictionary *settled = [originalRecord mutableCopy];
            settled[@"phase"] = @"completed";
            settled[@"restored_by"] = request[@"id"];
            // Preserve the original response when present; never turn it into a
            // successful repeat-write result after restoring its effects.
            if (!settled[@"result"]) settled[@"result"] = @{@"error": @"interrupted_then_restored", @"id": request[@"restore_of"]};
            [self save:settled name:[self nameFor:request[@"restore_of"] user:user session:session]];
        }
        return result;
    } @catch (NSException *exception) {
        return @{@"error": [exception.name isEqual:@"RelayMutation"] ? exception.reason : @"operation_failed"};
    } @finally {
        [_preferences unlock];
        [_lock unlock];
    }
}
- (void)dealloc {
    if (_owner >= 0) close(_owner);
    if (_directory >= 0) close(_directory);
}
@end
