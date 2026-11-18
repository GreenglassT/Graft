// A stand-in for iOS's private Preferences.framework, for running a
// cyan-injected IPA under PlayCover on macOS.
//
// Tweak settings libraries (e.g. libexample.dylib) link
// /System/Library/PrivateFrameworks/Preferences.framework/Preferences and
// import the ObjC class PSTableCell from it. macOS has no such framework in
// any location, so dyld aborts the process at launch. `nm -u` on the library
// lists what it needs; the only other "preferences" symbols such libraries
// want (CFPreferencesAppSynchronize, CFPreferencesSetAppValue) come from
// CoreFoundation, which macOS does have. If a tweak imports more, extend this
// file.
//
// PSTableCell is a UITableViewCell subclass. This declares it and nothing
// else: enough for dyld to bind, and honest about being a stub — the cell is
// only ever built for a Settings pane that does not exist off-device.
#import <UIKit/UIKit.h>

// The ivar is declared, not just the class. The libraries seen so far read
// PSTableCell's `_specifier` directly, so dyld wants
// _OBJC_IVAR_$_PSTableCell._specifier as well as the class and metaclass --
// three symbols, which `nm -u | grep 'PS[A-Z]'` lists in full. Typed `id`
// rather than PSSpecifier*: nothing here dereferences it, and declaring the
// real type would mean stubbing that class too for no gain.
@interface PSTableCell : UITableViewCell {
    id _specifier;
}
@end

@implementation PSTableCell
@end
