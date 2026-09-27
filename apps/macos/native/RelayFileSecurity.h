#import <Foundation/Foundation.h>
#include <sys/acl.h>
#include <errno.h>

static inline BOOL RelayNoAllowACL(int descriptor) {
    errno = 0;
    acl_t acl = acl_get_fd_np(descriptor, ACL_TYPE_EXTENDED);
    // For an open, stat-checked descriptor Darwin reports an absent ACL as ENOENT.
    if (!acl) return errno == ENOENT;
    BOOL safe = YES;
    for (int index = 0; ; index++) {
        acl_entry_t entry;
        errno = 0;
        // Darwin returns -1/EINVAL at the end, unlike the Linux ACL API.
        if (acl_get_entry(acl, index, &entry) != 0) { safe = errno == EINVAL; break; }
        acl_tag_t tag;
        if (acl_get_tag_type(entry, &tag) != 0 || tag != ACL_EXTENDED_DENY) { safe = NO; break; }
    }
    acl_free(acl);
    return safe;
}
