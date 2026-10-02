"""Defects found by hand-auditing the tuned-on held-out answers (nanopb, NModbus, uC/OS-II, Paho): returns that only
look unconditional (compiled out, or under a brace-less if), a returning macro worded as a write, failure tails that
ran into the next `case`, decisions inside a macro's expansion (a `?:`, a `do { } while (0)`), the side of an equality
the question names, follow-on calls taken from another catch handler, a caller's success test, writes made by a macro,
and unit tests answering for the program."""

import pytest

from conftest import ask

C_FILES = {
    "codec/stream.h": """
#define SET_ERROR(s, m) ((s)->errmsg = (s)->errmsg ? (s)->errmsg : (m))
#define RETURN_ERROR(s, m) return SET_ERROR(s, m), 0
#define tadd(a, b, r) do { (r)->v = (a)->v + (b)->v; if ((r)->v > 1000) (r)->v -= 1000; } while (0)
typedef struct stream_s {
    const char *errmsg;
    unsigned left;
    unsigned written;
    unsigned max;
    int (*cb)(struct stream_s *s, const unsigned char *buf, unsigned n);
} stream_t;
typedef struct { int v; } tv_t;
""",
    "codec/encode.c": """
#include "stream.h"
int buf_write(stream_t *s, const unsigned char *buf, unsigned n) { (void)s; (void)buf; (void)n; return 1; }
int out_write(stream_t *s, const unsigned char *buf, unsigned count)
{
    if (count > 0 && s->cb != 0)
    {
        if (s->written + count > s->max)
        {
            RETURN_ERROR(s, "stream full");
        }
#ifdef OUT_BUFFER_ONLY
        if (!buf_write(s, buf, count))
            RETURN_ERROR(s, "io error");
#else
        if (!s->cb(s, buf, count))
            RETURN_ERROR(s, "io error");
#endif
    }
    s->written += count;
    return 1;
}
void stream_init(stream_t *s) { s->cb = buf_write; s->errmsg = 0; s->written = 0; s->max = 64; }
""",
    "codec/decode.c": """
#include "stream.h"
int in_read(stream_t *s, unsigned char *buf, unsigned count)
{
    if (s->left < count)
        RETURN_ERROR(s, "end-of-stream");
    s->left -= count;
    (void)buf;
    return 1;
}
int in_skip(stream_t *s)
{
    if (s->left != 0)
        RETURN_ERROR(s, "trailing data");
    return 1;
}
int decode_field(stream_t *s, int ltype, int wire)
{
    switch (ltype)
    {
        case 1:
            if (wire != 0)
                RETURN_ERROR(s, "wrong wire type");
            return in_skip(s);
        case 2:
            if (wire != 2)
                RETURN_ERROR(s, "wrong wire type");
            return in_read(s, 0, 4);
        default:
            RETURN_ERROR(s, "invalid field type");
    }
}
int stream_check(stream_t *s) { return s->errmsg ? 0 : 1; }
void countdown(tv_t *t, tv_t *now)
{
    tv_t iv;
    iv.v = 5;
    tadd(now, &iv, t);
}
""",
    "can/can.c": """
#define CAN_ERR_BUS_OFF 0x04u
int can_bus_off_count;
int sync_error;
void can_status(unsigned st)
{
    if ((st & CAN_ERR_BUS_OFF) != 0u) {
        can_bus_off_count++;
    } else {
        can_bus_off_count = 0;
    }
}
void sync_receive(unsigned char dlc_in, unsigned char counter)
{
    unsigned char DLC = dlc_in;
    if (DLC == 1U) {
        sync_error = 0;
        (void)counter;
    } else {
        sync_error = DLC | 0x40U;
    }
}
""",
    "codec/required.c": """
#include "stream.h"
int encode_required(stream_t *s, int required, int present)
{
    if (required && !present)
        RETURN_ERROR(s, "missing required field");
    return 1;
}
int decode_required(stream_t *s, unsigned seen, unsigned all)
{
    if (seen != all)
        RETURN_ERROR(s, "missing required field");
    return 1;
}
""",
    "link/frame.c": """
typedef int BOOL;
#define TRUE 1
#define FALSE 0
unsigned frames_ok, crc_errors;
void dispatch(const unsigned char *f) { (void)f; }
BOOL parse_frame(const unsigned char *buf, unsigned n)
{
    unsigned crc = 0, i;
    for (i = 0; i + 1 < n; i++) crc ^= buf[i];
    if (crc != buf[n - 1]) {
        crc_errors++;
        return FALSE;
    }
    return TRUE;
}
void reader(const unsigned char *buf, unsigned n)
{
    if (parse_frame(buf, n)) {
        frames_ok++;
        dispatch(buf);
    }
}
""",
    "units/units.c": """
#define ERR_TOO_MANY -4
#define MAX_UNITS 8
int units_cfg;
int detect_units(int found)
{
    if (found < MAX_UNITS) {
        return found;
    }
    return ERR_TOO_MANY;
}
int config_units(int found)
{
    int n = detect_units(found);
    if (n > 0) {
        units_cfg = n;
    }
    return n;
}
""",
    "nmea/nmea.c": """
enum kind { K_INVALID = -1, K_RMC = 1 };
int checksum_ok(const char *s) { return s[0] == '$'; }
enum kind sentence_kind(const char *s)
{
    if (!checksum_ok(s))
        return K_INVALID;
    return K_RMC;
}
void handle_rmc(const char *s) { (void)s; }
void on_line(const char *s)
{
    switch (sentence_kind(s)) {
        case K_RMC:
            handle_rmc(s);
            break;
        case K_INVALID:
            break;
        default:
            break;
    }
}
""",
    "coe/sdo.c": """
int sdo_errors, pdo_count;
int sdo_read(int ok_frame, int is_abort)
{
    int wkc = 1;
    if (!ok_frame) {
        if (is_abort) {
            sdo_errors++;
        }
        wkc = 0;
    }
    return wkc;
}
int pdo_assign(int ok_frame)
{
    int wkc, n = 0;
    wkc = sdo_read(ok_frame, 1);
    if ((wkc > 0) && (n == 0)) {
        pdo_count = 1;
    }
    return n;
}
""",
    "kernel/task.c": """
#define ERR_NONE    0
#define ERR_NO_TCB 66
int tcb_table[8];
static int tcb_free = 0;
int tcb_take(int prio)
{
    if (tcb_free > 0) {
        tcb_free--;
        tcb_table[prio] = 1;
        return ERR_NONE;
    }
    return ERR_NO_TCB;
}
void sched(void) { }
int task_make(int prio)
{
    int err;
    tcb_table[prio] = 2;
    err = tcb_take(prio);
    if (err == ERR_NONE) {
        sched();
    } else {
        tcb_table[prio] = 0;
    }
    return err;
}
""",
}

CS_FILES = {
    "Link/Transport.cs": """
using System;
namespace Link
{
    public class SlaveException : Exception { public SlaveException(string m) : base(m) { } }
    public enum Codes { Acknowledge = 5, Busy = 6 }
    public class Response { public Codes Code; }
    public class Transport
    {
        private int _retries = 3;
        public void Log(string m) { }
        public void Pause(int ms) { }
        private Response Read() { return new Response(); }
        private bool ShouldRetry(Response r) { return false; }
        private void Validate(Response r) { }

        public void Unicast()
        {
            int attempt = 1;
            bool success = false;
            do
            {
                try
                {
                    bool readAgain;
                    Response r;
                    do
                    {
                        readAgain = false;
                        r = Read();
                        if (r != null)
                        {
                            readAgain = r.Code == Codes.Acknowledge;
                            if (readAgain)
                            {
                                Log("acknowledge received, reading again");
                                Pause(10);
                            }
                            else
                            {
                                throw new SlaveException("slave error");
                            }
                        }
                        else if (ShouldRetry(r))
                        {
                            readAgain = true;
                        }
                    }
                    while (readAgain);
                    Validate(r);
                    success = true;
                }
                catch (SlaveException se)
                {
                    if (attempt++ > _retries)
                        throw;
                    Pause(20);
                }
                catch (Exception e)
                {
                    Log("failed " + e.Message);
                    throw;
                }
            }
            while (!success);
        }
    }
}
""",
    "Link.UnitTests/TransportFixture.cs": """
namespace Link.UnitTests
{
    public class TransportFixture
    {
        public void Unicast_Retries_Acknowledge()
        {
            var t = new Link.Transport();
            t.Unicast();
        }
    }
}
""",
}


def _make(core, tmp_path_factory, name, files):
    d = tmp_path_factory.mktemp(name) / name
    for rel, text in files.items():
        f = d / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    j = core.start_index(str(d), full=True)
    assert core.wait(j["id"], 600)["state"] == "done"
    return d


@pytest.fixture(scope="module")
def c_repo(core, tmp_path_factory):
    return _make(core, tmp_path_factory, "auditc", C_FILES)


@pytest.fixture(scope="module")
def cs_repo(core, tmp_path_factory):
    return _make(core, tmp_path_factory, "auditcs", CS_FILES)


def _text(core, repo, q):
    return " ".join(ask(core, repo, q)["answer"])


def test_compiled_out_return_does_not_end_the_side(core, c_repo):
    # `#ifdef OUT_BUFFER_ONLY … RETURN_ERROR(…)` is compiled out: the outer check's side goes on to the callback
    text = _text(core, c_repo, "What happens when the output buffer is full while writing?")
    assert "out_write" in text and "stream full" in text, text
    assert "rest of out_write() is skipped" not in text, text


def test_returning_macro_is_said_once(core, c_repo):
    text = _text(core, c_repo, "What happens when the output buffer is full while writing?")
    assert 'returns through `RETURN_ERROR(s, "stream full")`' in text, text
    assert "sets `RETURN_ERROR" not in text, text


def test_more_than_left_is_an_ordering_test(core, c_repo):
    text = _text(core, c_repo, "What happens when the decoder needs more bytes than are left in the input?")
    assert "in_read" in text and "s->left < count" in text and "end-of-stream" in text, text


def test_failure_tail_stops_at_return_and_case_label(core, c_repo):
    text = _text(core, c_repo, "What happens when a field arrives with the wrong wire type?")
    first = text.split("Because it returns early")[0]
    assert "decode_field" in first and "wrong wire type" in first, text
    assert "invalid field type" not in first and "then it checks" not in first, text


def test_macro_ternary_is_not_a_consequence(core, c_repo):
    text = _text(core, c_repo, "What happens when the decoder needs more bytes than are left in the input?")
    assert "Consequence" not in text, text


def test_field_written_by_a_macro(core, c_repo):
    text = _text(core, c_repo, "Where is the stream error message errmsg set?")
    assert "set by the macro SET_ERROR" in text and "RETURN_ERROR" in text, text


def test_loop_inside_a_macro_is_not_a_step(core, c_repo):
    text = _text(core, c_repo, "What does countdown do?")
    assert "repeats while" not in text, text


def test_caller_success_test_takes_the_failure_side(core, c_repo):
    text = _text(core, c_repo, "What happens when there are no free task control blocks left?")
    assert "tcb_take" in text and "ERR_NO_TCB" in text, text
    assert "when that is false it sets `tcb_table[prio] = 0`" in text and "sched" not in text, text


def test_named_value_of_an_equality_picks_its_side(core, cs_repo):
    # `readAgain = r.Code == Codes.Acknowledge; if (readAgain) {…} else throw` — ACKNOWLEDGE is the TRUE side
    text = _text(core, cs_repo, "What happens when the slave responds with an ACKNOWLEDGE exception?")
    first = text.split("When it does not return there")[0]
    assert "readAgain" in first and "Pause" in first and "NOT true" not in first, text
    assert "Codes.Acknowledge" in first, text                 # what readAgain tests is shown with it


def test_follow_on_calls_skip_other_handlers_and_sides(core, cs_repo):
    text = _text(core, cs_repo, "What happens when the slave responds with an ACKNOWLEDGE exception?")
    nxt = text.split("When it does not return there", 1)[1] if "When it does not return there" in text else ""
    assert "ShouldRetry" not in nxt and "Log" not in nxt, text


def test_unit_test_does_not_answer_for_the_program(core, cs_repo):
    text = _text(core, cs_repo, "How does the transport unicast retry on acknowledge?")
    assert "TransportFixture" not in text.split(".")[0] and "Transport.Unicast()" in text, text


def test_bit_test_is_not_an_equality_with_the_flag(core, c_repo):
    text = _text(core, c_repo, "What happens when the CAN controller goes bus-off?")
    assert "can_status" in text and "can_bus_off_count++" in text and "NOT true" not in text, text


def test_capitalised_local_is_not_a_named_value(core, c_repo):
    # `DLC == 1U`: DLC is a local (all capitals), not a value the question can name; `(st & FLAG) != 0u` is a bit test
    from verity.agent.recipes import _question_side
    from verity.analysis.graph import CodeGraph
    g = CodeGraph(core.project(str(c_repo)).store, c_repo)

    def side(fn_name, line, q):
        fn = next(k for k in g.by_name[fn_name] if k in g.defined)
        br = next(b for b in g.branches[fn] if b["line"] == line)
        return _question_side(g, fn, br, {"sync", "message", "wrong", "length", "dlc", "can", "bus", "off"}, q)
    assert side("sync_receive", 16, "What happens when a SYNC message has the wrong length?") == 0
    assert side("can_status", 7, "What happens when the CAN controller goes bus-off?") == 0


def test_decoding_question_is_answered_by_the_decoder(core, c_repo):
    text = _text(core, c_repo, "What happens when a decoded message is missing a required field?")
    assert "decode_required" in text[:200], text


def test_bool_result_fails_on_the_false_side_of_the_caller(core, c_repo):
    # `if (parse_frame(…)) { frames_ok++; dispatch(…); }`: a bad CRC makes parse_frame() return FALSE — skipped
    text = _text(core, c_repo, "What happens when a frame has a bad CRC?")
    assert "parse_frame" in text and "crc_errors++" in text, text
    assert "fails that is false, so it skips" in text and "then calls dispatch" not in text, text


def test_caller_test_is_evaluated_with_the_failing_value(core, c_repo):
    # `n = detect_units(…); if (n > 0) {…}` with ERR_TOO_MANY = -4: the test is false, the configuration is skipped
    text = _text(core, c_repo, "What happens when there are more units than MAX_UNITS?")
    assert "ERR_TOO_MANY" in text and "fails that is false, so it skips `units_cfg = n`" in text, text


def test_switch_on_the_result_is_not_an_if(core, c_repo):
    text = _text(core, c_repo, "What happens when a sentence has an invalid checksum?")
    assert "checks `sentence_kind(s)`" not in text and "then calls handle_rmc" not in text, text


def test_failing_value_held_in_a_local_reaches_the_callers(core, c_repo):
    # `wkc = 0; … return wkc;` and the caller tests `(wkc > 0) && …`: false, so the caller skips its work
    text = _text(core, c_repo, "What happens when a slave answers an SDO read with an abort?")
    assert "sdo_read" in text and "fails that is false, so it skips `pdo_count = 1`" in text, text


def test_fact_numbers_never_reach_the_reader(core, c_repo):
    from test_phase4 import FakeProse, _ask_with
    m = FakeProse("When the output buffer is full, `out_write()` returns through `RETURN_ERROR(s, \"stream full\")` "
                  "(fact 2); the callers are listed in fact 3.")
    ans = _ask_with(core, c_repo, "What happens when the output buffer is full while writing?", m)
    assert ans["meta"]["prose"]["mode"] == "model+facts", ans["meta"]["prose"]
    assert "fact 2" not in ans["answer"][0] and "fact 3" not in ans["answer"][0] and "listed below" in ans["answer"][0]
