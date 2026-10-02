"""Idioms found in real open-source C (lwIP, FreeRTOS, libmodbus) and handled generically: `goto` error exits, errno,
macro-generated increments, structs laid over buffers, nested field copies, function-pointer arity, thread-start
wrappers, test/model copies of a function, acronyms in questions, and names that do not exist."""
import pytest

from conftest import ask
from verity.retrieval.cards import split_ident
from verity.retrieval.search import _looks_like_identifier, expand_words
from verity.pathkinds import is_secondary_path
from verity.extract.recon import add_stub_declarations

FILES = {
    "src/net.h": """
#include <errno.h>
#include <string.h>
struct stats { int drop; int chkerr; };
extern struct stats g_stats;
#define STATS_INC(x) ++g_stats.x
struct pbuf { void *payload; int len; };
struct addr4 { unsigned addr; };
struct msg { int op; struct addr4 yiaddr; };
struct lease { struct addr4 offered; };
typedef void (*thread_fn)(void *arg);
int os_create_thread(void (*start)(void *), void *arg);   /* the platform API (unknown to the pack) */
""",
    "src/net.c": """
#include "net.h"
#include <pthread.h>
struct stats g_stats;
struct lease g_lease;
static unsigned short sum16(const void *p, int n) { const unsigned char *b = p; unsigned s = 0; while (n--) s += *b++; return (unsigned short)s; }
void pbuf_free(struct pbuf *p) { (void)p; }

int seg_input(struct pbuf *p)
{
    unsigned short chksum = sum16(p->payload, p->len);
    if (chksum != 0) {
        STATS_INC(chkerr);
        goto dropped;
    }
    return 0;
dropped:
    STATS_INC(drop);
    pbuf_free(p);
    return -1;
}

int set_rate(int rate)
{
    if (rate <= 0) {
        errno = EINVAL;
        return -1;
    }
    return rate;
}

static void handle_offer(struct msg *m) { g_lease.offered.addr = m->yiaddr.addr; }
void lease_recv(struct pbuf *p) { struct msg *m; m = (struct msg *)p->payload; handle_offer(m); }

struct tdata { thread_fn fn; void *arg; };
static void *trampoline(void *a) { struct tdata *d = a; d->fn(d->arg); return 0; }   /* runs what it was given */
int sys_thread_new(const char *name, thread_fn fn, void *arg)
{
    pthread_t t; static struct tdata d; (void)name; d.fn = fn; d.arg = arg;
    return pthread_create(&t, 0, trampoline, &d);
}
static void worker_main(void *arg) { (void)arg; }
void start_workers(void) { sys_thread_new("worker", worker_main, 0); }

typedef int (*input_fn)(struct pbuf *p, int flags);
static int two_args(struct pbuf *p, int flags) { (void)p; return flags; }
static void no_args(void) { }
void *g_any;
int dispatch(struct pbuf *p)
{
    input_fn f;
    g_any = (void *)two_args;
    g_any = (void *)no_args;          /* both flow through the same void* */
    f = (input_fn)g_any;
    return f(p, 1);
}
""",
    "test/model_net.c": """
#include "../src/net.h"
int seg_input(struct pbuf *p) { (void)p; return 0; }   /* a test double of the real function */
""",
}


@pytest.fixture(scope="module")
def rw_repo(core, tmp_path_factory):
    d = tmp_path_factory.mktemp("rwpat") / "rwpat"
    for rel, text in FILES.items():
        f = d / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    j = core.start_index(str(d), full=True)
    assert core.wait(j["id"], 600)["state"] == "done"
    return d


def _store(core, repo):
    return core.project(str(repo)).store


def test_goto_error_exit_is_followed(core, rw_repo):
    ans = ask(core, rw_repo, "What happens when a segment has a bad checksum?")
    text = " ".join(ans["answer"])
    assert "seg_input" in text and "chksum != 0" in text
    assert "dropped" in text and "pbuf_free" in text and "chkerr" in text, text


def test_macro_increment_is_a_write(core, rw_repo):
    st = _store(core, rw_repo)
    roles = {r["role"] for r in st.q("SELECT role FROM occ WHERE sym='c:@S@stats@FI@chkerr'")}
    assert "rw" in roles


def test_errno_is_a_variable_not_a_call(core, rw_repo):
    st = _store(core, rw_repo)
    assert st.one("SELECT 1 FROM occ WHERE sym='c:@errno' AND role='write'")
    ans = ask(core, rw_repo, "What does set_rate do when the rate is not positive?")
    text = " ".join(ans["answer"])
    assert "EINVAL" in text and "__error" not in text and "__errno" not in text, text


def test_overlay_cast_and_outer_field_flows(core, rw_repo):
    st = _store(core, rw_repo)
    kinds = {(r["src"], r["dst"], r["kind"]) for r in st.q("SELECT src, dst, kind FROM flow")}
    assert ("c:@S@pbuf@FI@payload", "c:@S@msg@FI@yiaddr", "overlay") in kinds
    assert ("c:@S@msg@FI@yiaddr", "c:@S@lease@FI@offered", "member") in kinds


def test_function_pointer_targets_must_take_the_arguments(core, rw_repo):
    st = _store(core, rw_repo)
    targets = {r["callee"] for r in st.q("SELECT callee FROM call WHERE caller='c:@F@dispatch' AND derived=1")}
    assert any("two_args" in t for t in targets), targets
    assert not any("no_args" in t for t in targets), targets


def test_thread_start_wrapper_is_recognised(core, rw_repo):
    st = _store(core, rw_repo)
    e = st.one("SELECT * FROM entry WHERE sym LIKE '%worker_main' AND kind='thread'")
    assert e is not None and "sys_thread_new" in e["detail"] and "pthread_create" in e["detail"]
    t = st.one("SELECT * FROM entry WHERE sym LIKE '%trampoline' AND kind='thread'")
    assert t is not None and t["res"] == "trampoline"


def test_program_definition_wins_over_test_copy(core, rw_repo):
    st = _store(core, rw_repo)
    assert st.one("SELECT file FROM symbol WHERE key='c:@F@seg_input'")["file"] == "src/net.c"


def test_missing_name_is_said_plainly(core, rw_repo):
    ans = ask(core, rw_repo, "What does seg_output_all do?")
    assert ans["answer"][0].startswith("There is no `seg_output_all` in this code"), ans["answer"]


def test_words_and_paths():
    assert not _looks_like_identifier("TCP") and _looks_like_identifier("EPWM1") and _looks_like_identifier("MAX_LEN")
    assert split_ident("crc16") == ["crc16", "crc"] and "ip" in split_ident("ip4_input")
    assert {"chksum", "crc"} <= expand_words({"checksum"}) and "forward" in expand_words({"forwarded"})
    assert "head" not in expand_words({"header"})
    assert is_secondary_path("test/unit/tcp/test_tcp.c") and is_secondary_path("contrib/Coverity/coverity.c")
    assert is_secondary_path("doc/ZeroCopyRx.c") and not is_secondary_path("src/core/tcp_in.c")


def test_assigned_register_is_stubbed_as_variable(tmp_path):
    pre = tmp_path / "p.h"
    pre.write_text("")
    add_stub_declarations(str(pre), ["use of undeclared identifier 'IER'", "use of undeclared identifier 'PIEACK_GROUP1'"],
                          "IER = 0x0000; IFR |= M_INT1; x = PIEACK_GROUP1;", set())
    out = pre.read_text()
    assert "extern volatile unsigned long IER;" in out and "#define PIEACK_GROUP1 1" in out


# ---------------------------------------------------------------------------------------------- optional features
CFG_FILES = {
    "inc/cfg.h": """
#ifndef FEAT_CRC
#define FEAT_CRC (0)
#endif
#define FEAT_CRC_ENABLE   0x01
#define FEAT_CRC_EXTERNAL 0x02
#ifndef USE_LOG
#define USE_LOG 0
#endif
""",
    "src/crc.c": """
#include "../inc/cfg.h"
#if ((FEAT_CRC) & FEAT_CRC_ENABLE) != 0
#if ((FEAT_CRC) & FEAT_CRC_EXTERNAL) == 0
unsigned crc_calc(const unsigned char *b, int n) { unsigned c = 0; while (n--) c += *b++; return c; }
#endif
#endif
int frame_ok(const unsigned char *b, int n) {
#if ((FEAT_CRC) & FEAT_CRC_ENABLE) != 0
    return crc_calc(b, n) == 0;
#else
    return n > 0;
#endif
}
#if 0
void legacy_dump(void) { }
#endif
""",
}


@pytest.fixture(scope="module")
def cfg_repo(core, tmp_path_factory):
    d = tmp_path_factory.mktemp("cfgpat") / "cfgpat"
    for rel, text in CFG_FILES.items():
        f = d / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    j = core.start_index(str(d), full=True)
    assert core.wait(j["id"], 600)["state"] == "done"
    return d


def test_feature_options_are_found_with_positive_flags_only(core, cfg_repo):
    knobs = _store(core, cfg_repo).meta_get("c_profile")["knobs"]
    assert knobs["FEAT_CRC"]["value"] == "(FEAT_CRC_ENABLE)"          # never the …_EXTERNAL flag that removes code
    assert knobs["USE_LOG"]["value"] == "1"


def test_code_compiled_out_by_default_is_answered_with_a_caveat(core, cfg_repo):
    st = _store(core, cfg_repo)
    assert st.one("SELECT cfg FROM symbol WHERE name='crc_calc'")["cfg"] == "+ALL_FEATURES"
    ans = ask(core, cfg_repo, "Who calls crc_calc?")
    text = " ".join(ans["answer"])
    assert "compiled only when the build switches on optional features" in text and "frame_ok" in text, text


def test_name_in_never_compiled_code_is_not_claimed_absent(core, cfg_repo):
    ans = ask(core, cfg_repo, "What does legacy_dump do?")
    text = " ".join(ans["answer"])
    assert "There is no" not in text and "appears in the source" in text, text
