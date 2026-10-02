"""Phase 4: idioms found in uC/OS-II and Paho MQTT (held-out #5/#6) and the grounded re-ranker — success-first code
whose failure is the tail after the `if`, results handed back through `*perr`, a status `switch` whose case is the
timeout, a pointer filled by a callee (cyclic value flow), acronyms in questions, and the local model choosing among
verified candidates only."""
import json

import pytest

from conftest import ask
from verity.retrieval.search import expand_words

FILES = {
    "os/os.h": """
#define ERR_NONE          0u
#define ERR_NO_FREE_BLKS 93u
#define ERR_PRIO_EXIST   40u
#define ERR_NO_MORE_TCB  66u
#define ERR_TIMEOUT      10u
#define ST_OK 0u
#define ST_TO 1u
#define MAX_ID 65535
typedef struct pool { void *free_list; int nfree; } pool_t;
typedef struct os_tcb { int prio; int stat; int dly; struct os_tcb *next; } OS_TCB;
extern OS_TCB *cur;
extern OS_TCB *prio_tbl[64];
extern OS_TCB *free_tcbs;
void sched(void);
""",
    "os/core.c": """
#include "os.h"
OS_TCB *cur;
OS_TCB *prio_tbl[64];
OS_TCB *free_tcbs;
OS_TCB *tcb_list;
unsigned char cpu_usage;
unsigned long idle_ctr, idle_max;

void sched(void) { }

/* Get a memory block from a pool */
void *pool_get(pool_t *p, unsigned char *perr)
{
    if (p->nfree > 0) {
        void *b = p->free_list;
        p->free_list = *(void **)b;
        p->nfree--;
        *perr = ERR_NONE;
        return b;
    }
    *perr = ERR_NO_FREE_BLKS;
    return (void *)0;
}

unsigned char tcb_init(int prio)
{
    OS_TCB *t = free_tcbs;
    if (t != (OS_TCB *)0) {
        free_tcbs = t->next;
        t->prio = prio;
        prio_tbl[prio] = t;
        return ERR_NONE;
    }
    return ERR_NO_MORE_TCB;
}

unsigned char task_create(int prio)
{
    if (prio_tbl[prio] == (OS_TCB *)0) {
        unsigned char err = tcb_init(prio);
        sched();
        return err;
    }
    return ERR_PRIO_EXIST;
}

void time_tick(void)
{
    OS_TCB *t = tcb_list;
    while (t != (OS_TCB *)0) {
        if (t->dly != 0) {
            t->dly--;
            if (t->dly == 0) {
                t->stat = ST_TO;
            }
        }
        t = t->next;
    }
}

void sem_pend(int timeout, unsigned char *perr)
{
    cur->dly = timeout;
    sched();
    switch (cur->stat) {
        case ST_OK:
            *perr = ERR_NONE;
            break;
        case ST_TO:
        default:
            cur->dly = 0;
            *perr = ERR_TIMEOUT;
            break;
    }
}

void stat_task(void)
{
    int usage;
    usage = 100 - (int)(idle_ctr / idle_max);
    if (usage >= 0) {
        cpu_usage = (unsigned char)usage;
    } else {
        cpu_usage = 0;
    }
}

static int next_id;
int get_next_packet_id(void) { return next_id = (next_id == MAX_ID) ? 1 : next_id + 1; }
""",
    "net/packet.c": """
typedef struct { char *data; int len; } lstr;
typedef struct { lstr *topicName; } msgdata;
int read_lstr(lstr *s, unsigned char **pp) { s->len = (*pp)[0]; s->data = (char *)*pp + 1; return 1; }
int deserialize_publish(lstr *topic, unsigned char *buf) { unsigned char *p = buf; return read_lstr(topic, &p); }
static void make_md(msgdata *md, lstr *t) { md->topicName = t; }
void handler(msgdata *md);
void deliver(lstr *t) { msgdata md; make_md(&md, t); handler(&md); }
void cycle(unsigned char *buf) { lstr topic; if (deserialize_publish(&topic, buf) != 1) return; deliver(&topic); }
""",
}


@pytest.fixture(scope="module")
def p4_repo(core, tmp_path_factory):
    d = tmp_path_factory.mktemp("p4") / "p4"
    for rel, text in FILES.items():
        f = d / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    j = core.start_index(str(d), full=True)
    assert core.wait(j["id"], 600)["state"] == "done"
    return d


def test_failure_tail_and_out_parameter(core, p4_repo):
    ans = ask(core, p4_repo, "What happens when a task asks for a memory block and the pool has none left?")
    text = " ".join(ans["answer"])
    assert "pool_get" in text and "NOT true" in text and "ERR_NO_FREE_BLKS" in text, text
    assert "*perr = ERR_NO_FREE_BLKS" in text and "to its caller" in text, text


def test_existing_priority_is_the_tail_of_create(core, p4_repo):
    text = " ".join(ask(core, p4_repo, "What happens when a task is created at a priority that is already taken?")["answer"])
    assert "task_create" in text and "ERR_PRIO_EXIST" in text, text


def test_acronym_in_question(core, p4_repo):
    text = " ".join(ask(core, p4_repo, "What happens when there are no free task control blocks left?")["answer"])
    assert "tcb_init" in text and "ERR_NO_MORE_TCB" in text, text


def test_switch_case_timeout_and_who_sets_it(core, p4_repo):
    text = " ".join(ask(core, p4_repo, "What happens when a task waiting on a semaphore times out?")["answer"])
    assert "sem_pend" in text and "ERR_TIMEOUT" in text and "ST_TO" in text, text
    assert "time_tick" in text and "t->stat = ST_TO" in text, text


def test_local_computation_is_shown(core, p4_repo):
    text = " ".join(ask(core, p4_repo, "How is the CPU usage percentage computed?")["answer"])
    assert "stat_task" in text and "idle_max" in text, text


def test_ternary_return_is_one_step(core, p4_repo):
    text = " ".join(ask(core, p4_repo, "How does the packet id wrap around?")["answer"])
    assert "get_next_packet_id" in text and "MAX_ID" in text and "sets `return" not in text, text


def test_pointer_filled_by_callee_is_traced(core, p4_repo):
    text = " ".join(ask(core, p4_repo, "Where does the topic name of a received message come from?")["answer"])
    assert "deserialize_publish" in text and "read_lstr" in text, text


def test_words():
    assert "subscribe" in expand_words({"subscription"}) and "create" in expand_words({"created"})
    assert "receive" in expand_words({"received"}) and "connect" in expand_words({"connection"})
    assert {"blk", "blks"} <= expand_words({"blocks"}) and "prio" in expand_words({"priority"})
    assert "useds" not in expand_words({"taken"}) and "q" in expand_words({"queue"})


def test_short_identifier_words_kept():
    from verity.agent.router import topic_words
    assert "id" in topic_words("How does the packet id wrap around?")


def test_grader_accepts_a_denial():
    from verity.bench.run import grade
    case = {"id": "x", "category": "honesty", "q": "What does FooRestart do?", "must_not_claim_defined": ["FooRestart"]}

    def g(text):
        ans = {"answer": [text], "evidence": [], "flow": [], "meta": {},
               "sections": {"reasoning": [], "verification": {"report": {}}}}
        return grade(case, ans, set())["checks"]["no_invention"]["ok"]
    assert g("There is no function named `FooRestart` in this code; the closest is Bar, which is called by Baz.")
    assert not g("FooRestart is called by Baz at line 5.")


class FakeLLM:
    """Stands in for the local model: always picks candidate `choice` (1-based)."""
    def __init__(self, choice):
        self.choice, self.calls = choice, []

    def available(self):
        return True, ""

    def chat(self, messages, **kw):
        self.calls.append(messages)
        return {"json": {"choice": self.choice, "why": "it matches the question"}}


def test_rerank_only_reorders_and_discloses(core, p4_repo):
    from verity.agent import rerank
    from verity.agent.recipes import Ctx, _rerank
    from verity.agent.router import route
    from verity.analysis.graph import CodeGraph
    from verity.retrieval.search import Searcher
    pr = core.project(str(p4_repo))
    g = CodeGraph(pr.store, p4_repo)
    s = Searcher(pr.store, None)
    cx = Ctx(g, s, pr.store, route("What happens when a task is created?", s, None))
    cx.llm = FakeLLM(2)
    items = ["a", "b", "c"]
    out = _rerank(cx, items, lambda x: f"candidate {x}")
    assert out == ["b", "a", "c"] and sorted(out) == sorted(items)
    assert any("The local model chose" in a for a in cx.pack.assumptions)
    # "none fits" / out of range keeps the deterministic order and adds nothing
    cx2 = Ctx(g, s, pr.store, route("What happens when a task is created?", s, None))
    cx2.llm = FakeLLM(0)
    assert _rerank(cx2, items, str) == items and not cx2.pack.assumptions
    assert rerank.pick(FakeLLM(9), "q", ["x", "y"]) == (None, "")
    # the prompt carries the candidates as code, and only them
    llm = FakeLLM(1)
    rerank.pick(llm, "why?", ["fn() at a.c:1 checks `x`", "fn() at a.c:2 checks `y`"])
    user = llm.calls[0][1]["content"]
    assert "[1] fn() at a.c:1" in user and "[2] fn() at a.c:2" in user


# ---------------------------------------------------------------------------------------------- C# failure idioms
CS_FILES = {
    "Net/Link.csproj": """<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup></Project>""",
    "Net/Link.cs": """
using System;
using System.Net.Sockets;
using System.Threading;

namespace Net
{
    public class LinkException : Exception { public LinkException(string m, Exception inner) : base(m, inner) { } }

    public class Link
    {
        private Socket _socket;
        private int _keepAlivePeriod;
        private Thread _keepAliveThread;
        private ushort _counter;

        public void Connect(int keepAlive)
        {
            try
            {
                _socket.Connect("broker", 1883);
            }
            catch (SocketException ex)
            {
                throw new LinkException("cannot connect to the broker", ex);
            }
            _keepAlivePeriod = keepAlive * 1000;
            if (_keepAlivePeriod != 0)
            {
                _keepAliveThread = new Thread(KeepAlive);
                _keepAliveThread.Start();
            }
        }

        public void Send(byte[] data)
        {
            if (data == null)
                throw new ArgumentNullException(nameof(data));
            try
            {
                _socket.Send(data);
            }
            catch (Exception e)
            {
                throw new LinkException("send failed", e);
            }
        }

        private int _retries = 3;

        public void Transfer(byte[] data)
        {
            int attempt = 1;
            while (true)
            {
                try
                {
                    Validate(data);
                    return;
                }
                catch (TimeoutException te)
                {
                    throw;
                }
                catch (Exception e)
                {
                    if (attempt++ > _retries)
                        throw;
                    Thread.Sleep(10);
                }
            }
        }

        private void Validate(byte[] data)
        {
            if (data.Length != 8)
                throw new System.IO.IOException("unexpected length");
        }

        private void KeepAlive() { }

        public ushort NextId()
        {
            _counter = ((_counter % ushort.MaxValue) != 0) ? (ushort)(_counter + 1) : (ushort)1;
            return _counter;
        }
    }
}
""",
}


@pytest.fixture(scope="module")
def cs4_repo(core, tmp_path_factory):
    d = tmp_path_factory.mktemp("cs4") / "cs4"
    for rel, text in CS_FILES.items():
        f = d / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    j = core.start_index(str(d), full=True)
    assert core.wait(j["id"], 600)["state"] == "done"
    return d


def test_catch_is_a_decision(core, cs4_repo):
    st = core.project(str(cs4_repo)).store
    assert st.one("SELECT 1 FROM branch WHERE kind='catch' AND cond='catch (SocketException)'")
    text = " ".join(ask(core, cs4_repo, "What happens when the client cannot connect to the broker?")["answer"])
    assert "Connect()" in text and "SocketException" in text and "LinkException" in text and "throws" in text, text


def test_catch_follows_the_try_block(core, cs4_repo):
    text = " ".join(ask(core, cs4_repo, "What happens when sending on the socket fails?")["answer"])
    assert "Send()" in text and "LinkException" in text, text


def test_guard_clause_is_an_if(core, cs4_repo):
    text = " ".join(ask(core, cs4_repo, "What does Send do?")["answer"])
    assert "data == null" in text and "throws `new ArgumentNullException(nameof(data))`" in text, text


def test_zero_state_is_the_skipped_side(core, cs4_repo):
    text = " ".join(ask(core, cs4_repo, "What happens when the keep alive period is zero?")["answer"])
    assert "_keepAlivePeriod != 0" in text and "skips" in text and "KeepAlive" in text, text
    assert "#ctor" not in text and "new Thread()" in text, text


def test_wrap_question_prefers_wrapping_code(core, cs4_repo):
    text = " ".join(ask(core, cs4_repo, "How does the id counter wrap around?")["answer"])
    assert "NextId()" in text and "MaxValue" in text, text


def test_secondary_paths():
    from verity.pathkinds import is_secondary_path, is_test_path
    assert is_secondary_path("contrib/oshw/erika/nicdrv.c") and not is_secondary_path("oshw/linux/nicdrv.c")
    assert is_test_path("TestMqtt/App/Program.cs") and not is_test_path("src/Testing.c")


def test_run_output_is_readable():
    from verity.agent.recipes import _readable_run_output
    out = _readable_run_output("__VR_ENTER 0x1\n__VR_BASE 0x2\n__VR_HW motor(5)\n__VR_PHASE after\n__VR_STATE g.x=3\nhello")
    assert "__SX" not in out and "hardware call: motor(5)" in out and "g.x = 3  (after)" in out and "hello" in out


class FakeProse:
    """A local model stand-in for the wording step: keeps the re-ranker's order and returns a fixed summary."""
    label, tok_per_s = "fake-model", 1.0

    def __init__(self, text):
        self.text, self.worded = text, 0

    def available(self):
        return True, ""

    def chat(self, messages, schema=None, **kw):
        if schema and "choice" in (schema.get("properties") or {}):
            return {"json": {"choice": 1, "why": "first"}}
        self.worded += 1
        return {"json": {"answer": self.text}, "seconds": 0.01}


def _ask_with(core, repo, q, model):
    from verity.agent.answer import Answerer
    pr = core.project(str(repo))
    a = Answerer(pr.store, repo)
    a._llm = lambda: model
    return a.ask(q, use_llm=True)


def test_model_summary_is_followed_by_the_facts(core, p4_repo):
    m = FakeProse("When the pool has none left, `pool_get()` reports `ERR_NO_FREE_BLKS` to its caller and returns "
                  "`(void *)0`.")
    ans = _ask_with(core, p4_repo, "What happens when a task asks for a memory block and the pool has none left?", m)
    assert ans["meta"]["prose"]["mode"] == "model+facts", ans["meta"]["prose"]
    assert ans["answer"][0].startswith("When the pool has none left") and any("NOT true" in p for p in ans["answer"][1:])


def test_paraphrased_condition_is_rejected(core, p4_repo):
    # the model "quotes" a condition the code does not contain: the checked template is shown instead
    m = FakeProse("When the pool has none left, `pool_get()` checks `p->nfree == 0` and reports `ERR_NO_FREE_BLKS`.")
    ans = _ask_with(core, p4_repo, "What happens when a task asks for a memory block and the pool has none left?", m)
    assert ans["meta"]["prose"]["mode"] == "direct" and "p->nfree == 0" in ans["meta"]["prose"].get("bad_quotes", [])
    assert not any("p->nfree == 0" in p for p in ans["answer"])


def test_not_found_is_never_reworded(core, p4_repo):
    m = FakeProse("Restart reboots the device.")
    ans = _ask_with(core, p4_repo, "What does task_restart do?", m)
    assert m.worded == 0 and ans["answer"][0].startswith("There is no `task_restart`"), ans["answer"]


def test_orphaned_model_server_is_reaped(tmp_path, monkeypatch):
    """A model server whose Verity owner died (crash, kill -9) is stopped at the next start."""
    import json as _json
    import os
    import shutil
    import subprocess
    import sys
    import time
    import verity.llm.runtime as rt
    # a stand-in process whose command line says llama-server (Windows: a copy, since symlinks need admin rights)
    if os.name == "nt":
        fake = tmp_path / "llama-server.exe"
        shutil.copy(os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "PING.EXE"), fake)
        wait_args = ["-n", "60", "127.0.0.1"]
    else:
        fake = tmp_path / "llama-server"
        fake.symlink_to("/bin/sleep")
        wait_args = ["60"]
    child = subprocess.Popen([str(fake)] + wait_args, stdout=subprocess.DEVNULL)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()                                      # an owner pid that no longer exists
    run = tmp_path / "run"
    run.mkdir()
    (run / f"{child.pid}.json").write_text(_json.dumps({"pid": child.pid, "owner": dead.pid, "model": "x.gguf"}))
    monkeypatch.setattr(rt, "_RUN_DIR", run)
    try:
        assert rt.reap_orphans() == [child.pid]
        for _ in range(20):
            if child.poll() is not None:
                break
            time.sleep(0.1)
        assert child.poll() is not None and not list(run.glob("*.json"))
    finally:
        if child.poll() is None:
            child.kill()
    # a server whose owner is alive (another running Verity) is left alone
    other = subprocess.Popen([str(fake)] + wait_args, stdout=subprocess.DEVNULL)
    try:
        (run / f"{other.pid}.json").write_text(_json.dumps({"pid": other.pid, "owner": os.getppid(), "model": "x"}))
        assert rt.reap_orphans() == [] and other.poll() is None
    finally:
        other.kill()


def test_catch_must_match_the_thrown_type(core, cs4_repo):
    text = " ".join(ask(core, cs4_repo, "What happens when Validate gets data whose length is wrong?")["answer"])
    assert "Validate()" in text and "IOException" in text, text
    assert "catch (Exception)" in text and "catch (TimeoutException)" not in text, text


def test_missing_field_is_malformed_input_not_lost_data(core, p4_repo):
    from verity.agent.router import route
    from verity.retrieval.search import Searcher
    s = Searcher(core.project(str(p4_repo)).store, None)
    assert route("What happens when a decoded message is missing a required field?", s, None).intent == "failure_invalid"
    assert route("What happens if the heartbeat is missing?", s, None).intent == "failure_missing"


def test_who_calls_uses_the_name_written_as_code(core, p4_repo):
    text = " ".join(ask(core, p4_repo, "Who calls tcb_init?")["answer"])
    assert "task_create" in text, text
