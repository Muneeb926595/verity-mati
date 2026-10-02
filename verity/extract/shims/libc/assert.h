#ifndef VR_ASSERT_H
#define VR_ASSERT_H
void __vr_assert_fail(const char *e, const char *f, int l);
#define assert(e) ((e) ? (void)0 : __vr_assert_fail(#e, __FILE__, __LINE__))
#endif
