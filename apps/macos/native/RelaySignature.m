#import "RelaySignature.h"
#import <Security/Security.h>
#include <mach-o/dyld.h>
#include <limits.h>

static NSString *const AppID = @"com.wangxinlei.relay";
static NSString *const HelperID = @"com.wangxinlei.relay.helper";
static NSString *const DeveloperRule = @"anchor apple generic and certificate 1[field.1.2.840.113635.100.6.2.6] exists and certificate leaf[field.1.2.840.113635.100.6.1.13] exists";

static NSString *Hex(NSData *data) {
    NSMutableString *value = [NSMutableString string];
    const unsigned char *bytes = data.bytes;
    for (NSUInteger i = 0; i < data.length; i++) [value appendFormat:@"%02x", bytes[i]];
    return value;
}
static NSDictionary *Projection(NSDictionary *info, BOOL valid, BOOL developer) {
    uint32_t flags = [info[(__bridge NSString *)kSecCodeInfoFlags] unsignedIntValue];
    uint32_t dynamic = [info[(__bridge NSString *)kSecCodeInfoStatus] unsignedIntValue];
    return @{@"valid": @(valid), @"developer_id": @(developer),
        @"adhoc": @((flags & kSecCodeSignatureAdhoc) != 0), @"hardened": @((flags & kSecCodeSignatureRuntime) != 0),
        @"debugged": @((dynamic & kSecCodeStatusDebugged) != 0),
        @"identifier": info[(__bridge NSString *)kSecCodeInfoIdentifier] ?: @"",
        @"team": info[(__bridge NSString *)kSecCodeInfoTeamIdentifier] ?: @"",
        @"cdhash": Hex(info[(__bridge NSString *)kSecCodeInfoUnique]),
        @"entitlements": info[(__bridge NSString *)kSecCodeInfoEntitlementsDict] ?: @{}};
}
static BOOL Safe(NSDictionary *identity, NSString *identifier) {
    if (![identity[@"valid"] boolValue] || ![identity[@"developer_id"] boolValue] ||
        ![identity[@"hardened"] boolValue] || [identity[@"debugged"] boolValue] ||
        [identity[@"adhoc"] boolValue] || ![identity[@"identifier"] isEqual:identifier]) return NO;
    NSString *team = identity[@"team"], *hash = identity[@"cdhash"];
    if ([team length] != 10 || [team rangeOfCharacterFromSet:[[NSCharacterSet characterSetWithCharactersInString:@"ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"] invertedSet]].location != NSNotFound ||
        hash.length != 40) return NO;
    for (NSString *key in @[@"com.apple.security.get-task-allow", @"com.apple.security.cs.disable-library-validation",
        @"com.apple.security.cs.allow-dyld-environment-variables", @"com.apple.security.cs.allow-unsigned-executable-memory",
        @"com.apple.security.cs.disable-executable-page-protection"]) {
        if ([identity[@"entitlements"][key] boolValue]) return NO;
    }
    return YES;
}
static NSString *Rule(NSDictionary *identity) {
    return [NSString stringWithFormat:@"%@ and identifier \"%@\" and certificate leaf[subject.OU] = \"%@\" and cdhash H\"%@\"",
        DeveloperRule, identity[@"identifier"], identity[@"team"], identity[@"cdhash"]];
}
NSDictionary *RelayRunningIdentity(void) {
    SecCodeRef code = NULL;
    CFDictionaryRef raw = NULL;
    if (SecCodeCopySelf(0, &code) != errSecSuccess) return @{};
    BOOL valid = SecCodeCheckValidity(code, kSecCSStrictValidate, NULL) == errSecSuccess;
    OSStatus status = SecCodeCopySigningInformation(code, kSecCSSigningInformation | kSecCSDynamicInformation, &raw);
    SecRequirementRef requirement = NULL;
    BOOL developer = NO;
    if (SecRequirementCreateWithString((__bridge CFStringRef)DeveloperRule, 0, &requirement) == errSecSuccess) {
        developer = SecCodeCheckValidity(code, 0, requirement) == errSecSuccess;
        CFRelease(requirement);
    }
    CFRelease(code);
    if (status != errSecSuccess || !raw) { if (raw) CFRelease(raw); return @{}; }
    return Projection(CFBridgingRelease(raw), valid, developer);
}
static NSDictionary *StaticInfo(NSString *path, NSString *identifier, NSString *team) {
    SecStaticCodeRef code = NULL;
    SecRequirementRef requirement = NULL;
    CFDictionaryRef info = NULL;
    NSString *rule = [NSString stringWithFormat:@"%@ and identifier \"%@\" and certificate leaf[subject.OU] = \"%@\"", DeveloperRule, identifier, team];
    BOOL valid = SecStaticCodeCreateWithPath((__bridge CFURLRef)[NSURL fileURLWithPath:path], 0, &code) == errSecSuccess &&
        SecRequirementCreateWithString((__bridge CFStringRef)rule, 0, &requirement) == errSecSuccess &&
        SecStaticCodeCheckValidity(code, kSecCSStrictValidate | kSecCSCheckAllArchitectures | kSecCSCheckNestedCode, requirement) == errSecSuccess &&
        SecCodeCopySigningInformation(code, kSecCSSigningInformation, &info) == errSecSuccess;
    if (requirement) CFRelease(requirement);
    if (code) CFRelease(code);
    if (!valid || !info) { if (info) CFRelease(info); return nil; }
    return CFBridgingRelease(info);
}
NSDictionary *RelayTrustedPair(void) {
    NSDictionary *own = RelayRunningIdentity();
    BOOL helper = [own[@"identifier"] isEqual:HelperID];
    if (!Safe(own, helper ? HelperID : AppID)) return @{};
    char executable[PATH_MAX], resolved[PATH_MAX];
    uint32_t size = sizeof(executable);
    if (_NSGetExecutablePath(executable, &size) || !realpath(executable, resolved)) return @{};
    NSString *path = [NSString stringWithUTF8String:resolved];
    NSString *suffix = helper ? @"/Contents/Library/LaunchServices/RelayHelper" : @"/Contents/MacOS/NetCare";
    if (![path hasSuffix:suffix]) return @{};
    NSString *bundle = [path substringToIndex:path.length - suffix.length];
    NSDictionary *appInfo = StaticInfo(bundle, AppID, own[@"team"]);
    NSDictionary *helperInfo = StaticInfo([bundle stringByAppendingString:@"/Contents/Library/LaunchServices/RelayHelper"], HelperID, own[@"team"]);
    if (!appInfo || !helperInfo) return @{};
    NSDictionary *appIdentity = Projection(appInfo, YES, YES), *helperIdentity = Projection(helperInfo, YES, YES);
    if (!Safe(appIdentity, AppID) || !Safe(helperIdentity, HelperID) ||
        ![own[@"cdhash"] isEqual:(helper ? helperIdentity : appIdentity)[@"cdhash"]] ||
        ![appInfo[(__bridge NSString *)kSecCodeInfoPList][@"RelayHelperCDHash"] isEqual:helperIdentity[@"cdhash"]]) return @{};
    return @{@"app_requirement": Rule(appIdentity), @"helper_requirement": Rule(helperIdentity),
        @"app_hash": appIdentity[@"cdhash"], @"helper_hash": helperIdentity[@"cdhash"], @"team": own[@"team"]};
}
