# Architecture, in pictures

Colours mean the same thing in every picture:
🟪 **an LLM call** · 🟩 **deterministic code** · 🟧 **a human** · ⬜ **data, stores, the bank app**

---

## 1. The big picture: learn once, then repeat without an LLM

```mermaid
---
config:
  look: handDrawn
  theme: base
  themeVariables:
    fontFamily: '"Comic Sans MS", "Comic Neue", "Chalkboard SE", cursive'
    fontSize: 16px
    lineColor: '#5F5E5A'
---
flowchart TB
  subgraph BUILD["🔍 Discovery phase"]
    direction LR
    D["🤖 Discovery agent<br/>the LLM drives the browser"] --> T["📝 Trace<br/>what was done and why"]
    T --> C["⚙️ Compiler<br/>canonical, robust steps"]
    C --> Y["📜 Capability YAML<br/>versioned + hashed, draft"]
    Y --> R["🧑‍⚖️ Reviewer<br/>draft → approved"]
  end

  subgraph RUN["🔁 Replay phase"]
    direction LR
    U["🧑‍💼 Teller<br/>asks a question"] --> A["🤖 Bank agent<br/>the LLM picks a tool"]
    A --> P["▶️ Replay engine<br/>deterministic"]
    P --> B[("🏦 Legacy bank app")]
  end

  BUILD == "📜 approved capabilities only" ==> RUN

  classDef llm fill:#EEEDFE,stroke:#534AB7,color:#26215C
  classDef code fill:#E1F5EE,stroke:#0F6E56,color:#04342C
  classDef human fill:#FAECE7,stroke:#993C1D,color:#4A1B0C
  classDef data fill:#F1EFE8,stroke:#5F5E5A,color:#2C2C2A
  class D,A llm
  class T,C,P code
  class R,U human
  class Y,B data
```

**The idea:** in the **discovery phase**, the LLM explores once per task, to *learn* how the
task is done in the bank's UI. The result is a plain file that a human approves. In the
**replay phase**, on every request, the task runs from that file, the same way every time,
with no model in the execution. Both phases share the policy allowlist, redaction, the tenant registry and
overrides, the handoff inbox, and the evidence logs.

---

## 2. Discovery: one look, one action, every turn

```mermaid
---
config:
  theme: base
  themeVariables:
    fontFamily: '"Comic Sans MS", "Comic Neue", "Chalkboard SE", cursive'
    fontSize: 15px
    actorBkg: '#E1F5EE'
    actorBorder: '#0F6E56'
    noteBkgColor: '#FAEEDA'
    noteBorderColor: '#854F0B'
---
sequenceDiagram
  autonumber
  actor E as 🧑‍💻 Engineer
  participant AG as ⚙️ Discovery agent
  participant BR as 🏦 Browser
  participant CL as 🤖 LLM
  participant PO as 🛡️ Policy
  actor OP as 🧑‍⚖️ Operator

  E->>AG: goal + start URL (nothing else)
  loop until finish, stuck or limit (30 steps / 10 min)
    AG->>BR: observe the screen
    BR-->>AG: roles, captions, frames, screenshot
    Note over AG: redact for the model:<br/>secrets, card numbers, SSNs masked
    AG->>CL: system prompt + tools + screen text + masked image
    CL-->>AG: ONE tool call + its reason
    AG->>PO: is this allowed?
    alt allowed
      PO-->>AG: ✅ allow
      AG->>BR: click / type / select / extract
      Note over AG: write a trace step
    else irreversible (e.g. Confirm)
      PO-->>AG: ✋ needs approval
      AG->>OP: pause, ask (inbox)
      OP-->>AG: approve / reject / take over
    end
  end
  AG-->>E: trace.json → compile → capability (draft)
```

**The LLM's input** is the screen as redacted text plus a masked screenshot. **Its output** is
exactly one action with a reason. Passwords go in through `type_secret`, by name only, so
the LLM never sees them.

---

## 3. Replay: no guessing allowed

```mermaid
---
config:
  look: handDrawn
  theme: base
  themeVariables:
    fontFamily: '"Comic Sans MS", "Comic Neue", "Chalkboard SE", cursive'
    fontSize: 16px
    lineColor: '#5F5E5A'
---
flowchart TD
  IN["📥 Caller gives:<br/>capability + inputs + URL"] --> ID(["🔍 Which bank is this?<br/>match the first screen's signature"])
  ID -- "known" --> OV["🧩 Apply that bank's override<br/>only if approved + pinned to this base"]
  ID -- "never seen" --> NEW["🆕 Register it in tenants.yaml<br/>for a human to review"] --> OV
  OV --> LOOP

  subgraph LOOP["🔁 For every step"]
    direction TB
    L["🎯 Find the element<br/>must match exactly ONE"] --> ACT["👆 Act"]
    ACT --> CHK(["👀 Check the screen<br/>expectation + outcome rules"])
    CHK -- "known hiccup (notice, error page)" --> REC["🩹 Recovery rule<br/>dismiss / reload, bounded"] --> L
  end

  CHK -- "all steps done" --> OK["✅ Success<br/>typed outputs"]
  CHK -- "'not found' etc." --> BIZ["📋 Business outcome<br/>an answer, not an error"]
  CHK -- "can't continue" --> FAIL["❌ Failed<br/>step + expected + observed + screenshot"]
  L -- "irreversible step" --> APP["✋ Needs approval<br/>→ operator"]

  classDef code fill:#E1F5EE,stroke:#0F6E56,color:#04342C
  classDef human fill:#FAECE7,stroke:#993C1D,color:#4A1B0C
  classDef data fill:#F1EFE8,stroke:#5F5E5A,color:#2C2C2A
  classDef good fill:#EAF3DE,stroke:#3B6D11,color:#173404
  classDef bad fill:#FCEBEB,stroke:#A32D2D,color:#501313
  class ID,OV,NEW,L,ACT,CHK,REC code
  class IN,BIZ data
  class APP human
  class OK good
  class FAIL bad
```

**Rule of thumb:** if the replay isn't sure, it stops and says why. It never clicks the
"closest" thing.

---

## 4. Handoff: passing the steering wheel

### Who is in control

```mermaid
---
config:
  look: handDrawn
  theme: base
  themeVariables:
    fontFamily: '"Comic Sans MS", "Comic Neue", "Chalkboard SE", cursive'
    fontSize: 16px
---
stateDiagram-v2
  direction LR
  [*] --> Automation
  Automation --> AwaitingHuman: 🙋 stuck / needs approval / unrecoverable
  AwaitingHuman --> Human: take
  AwaitingHuman --> Automation: approve ✅
  Human --> Automation: resume / done (human clicks recorded)
  AwaitingHuman --> Finished: reject / abort / timeout
  Human --> Finished: abort
  Automation --> Finished: done or failed
  Finished --> [*]

  Automation: 🤖 Automation drives
  AwaitingHuman: ⏸️ Paused, request open
  Human: 🧑 Human has the wheel (automation locked out)
  Finished: 🏁 Finished

  classDef code fill:#E1F5EE,stroke:#0F6E56,color:#04342C
  classDef human fill:#FAECE7,stroke:#993C1D,color:#4A1B0C
  classDef data fill:#F1EFE8,stroke:#5F5E5A,color:#2C2C2A
  class Automation code
  class AwaitingHuman, Human human
  class Finished data
```

### How the two sides talk

```mermaid
---
config:
  look: handDrawn
  theme: base
  themeVariables:
    fontFamily: '"Comic Sans MS", "Comic Neue", "Chalkboard SE", cursive'
    fontSize: 16px
    lineColor: '#5F5E5A'
---
flowchart LR
  OP["🧑‍⚖️ Operator CLI<br/>list · take · approve · resume · abort"] <-- "commands" --> IB[("📂 Inbox folder<br/>request · status · responses")]
  IB <-- "polls + updates status" --> AU["🤖 Automation process<br/>keeps the browser alive"]
  OP -. "clicks directly in the window" .-> BW["🖥️ The SAME live browser<br/>nothing is reopened"]
  AU --> BW

  classDef code fill:#E1F5EE,stroke:#0F6E56,color:#04342C
  classDef human fill:#FAECE7,stroke:#993C1D,color:#4A1B0C
  classDef data fill:#F1EFE8,stroke:#5F5E5A,color:#2C2C2A
  class AU code
  class OP human
  class IB,BW data
```

The automation and the operator are separate programs. The inbox is their mailbox, and the
browser window is shared. If the window gets closed, the run ends cleanly as `session_lost`.

---

## 5. The bank's AI agent: three things can happen

```mermaid
---
config:
  look: handDrawn
  theme: base
  themeVariables:
    fontFamily: '"Comic Sans MS", "Comic Neue", "Chalkboard SE", cursive'
    fontSize: 16px
    lineColor: '#5F5E5A'
---
flowchart TD
  Q["🧑‍💼 Teller: a question in plain English"] --> AG["🤖 Bank agent (LLM)<br/>sees: question + approved tool contracts<br/>never sees: URLs, bank names, passwords, screens"]
  AG -- "a read tool fits" --> R1["📖 member_savings_balance"] --> A1["💬 Answers from typed outputs<br/>or 'member doesn't exist'<br/>(evidence 19–21)"]
  AG -- "a write tool fits" --> W1["✍️ open_holiday_share"] --> A2["✋ Replay stops at Confirm<br/>'waiting for a person to approve'<br/>(evidence 22)"]
  AG -- "nothing fits" --> G1["🤷 report_unsupported"] --> A3["📝 Gap logged + discovery command printed<br/>a human runs discovery<br/>(evidence 23)"]
  A3 -. "new capability, once approved" .-> AG

  classDef llm fill:#EEEDFE,stroke:#534AB7,color:#26215C
  classDef code fill:#E1F5EE,stroke:#0F6E56,color:#04342C
  classDef human fill:#FAECE7,stroke:#993C1D,color:#4A1B0C
  classDef data fill:#F1EFE8,stroke:#5F5E5A,color:#2C2C2A
  class AG llm
  class R1,W1,A1 code
  class Q,A2 human
  class G1,A3 data
```

**The LLM decides *what* to do.** The catalog and replay decide *how*: deterministically, with
the bank's own rules and a human in charge of anything irreversible.
