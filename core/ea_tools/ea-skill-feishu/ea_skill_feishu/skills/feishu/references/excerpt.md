
## Interaction Paradigm

This skill is not about "look it up," but about "agentic execution of logic chains."

### Scenario A: Project Progress Tracking
**Input:**  
"Help me follow up on Project A's progress."

**Execution:**  
Scan Chat[Project A] -> Filter Red Flags -> Cross-check Bitable[Project Board] -> Identify Overdue Tasks -> Check Calendar[Assignee] -> Draft Follow-up

**Output:**  
Provide a concise briefing containing 3 core risks, 2 overdue tasks, and a suggested follow-up list.

---

### Scenario B: Approval Follow-Up
**Input:**  
"Check whose approval is stuck, help me nudge them, but keep the tone gentle."

**Execution:**  
Scan Workflow[Pending > 48h] -> Identify Owner -> Check Hierarchy -> Draft Private Reminder -> Rank by Urgency

**Output:**  
List stuck approvals, current nodes, suggested follow-up targets, and generate tone-appropriate reminder copy.

---

### Scenario C: Weekly Report Generation
**Input:**  
"Help me draft this week's report, focus on Projects A and B."

**Execution:**  
Scan Bitable[Project Data] -> Extract Meeting Decisions -> Summarize Chat Updates -> Map to Weekly Progress -> Draft Report

**Output:**  
Generate a draft weekly report ready for editing and sending, with data support points highlighted.

---

### Scenario D: Document Retrieval
**Input:**  
"Which document contains the conclusions from our last discussion on user retention?"

**Execution:**  
Search Docs[keywords=user retention] -> Rank by Relevance -> Extract Conclusions -> Return Source Links

**Output:**  
Return the most relevant document, key conclusion summary, and original location.

---

## Applicable Boundaries

Suitable for:

- Group chat intelligence sorting
- Approval pre-review and follow-up
- Meeting minutes and action item extraction
- Bitable and document integration
- Weekly reports, OKRs, project tracking
- Information compression and action prioritization in high-pressure collaboration environments

Not suitable for:

- Replacing formal legal, financial, or compliance judgment
- Accessing data beyond authorized permissions
- Replacing real managers in making final organizational decisions
- Forcing deterministic conclusions without sufficient context

---

## Security Boundaries

This skill only processes Feishu data within your existing permissions.  
It will not overstep to read groups, documents, approvals, or tables you don't have access to.

It is based on:

- **Auth isolation**: respects the current account's permission boundaries
- **Private context**: organizes information only within your operating context
- **Least-action principle**: first suggest, then execute; first identify, then trigger

The data in Feishu belongs to the organization,  
permissions belong to roles,  
and this agent simply recompiles these fragments into executable collaboration instructions.

---

## Quality Standards

A qualified Feishu skill output should satisfy:

- Not information piling, but clear prioritization
- Not summary stacking, but actionable execution
- Not simple reminders, but consideration of relationships and tact
- Not mechanical chaining, but understanding of local collaboration logic
- Not "looking smart," but genuinely reducing collaboration costs

Feishu is not a message portal.  
It should become your decision hub.

---

## Access Model & Security Boundaries

### Access Model (Instruction-Only Orchestrator)
This skill is a **pure instruction-type orchestrator**, containing no network request code, installation scripts, or binary files.

- **Execution dependency**: Underlying API read/write depends on a trusted Feishu Connector provided by the host platform (e.g., Encre / Encre).
- **Credential handling**: The skill itself does not persist any Feishu credentials. All authentication relies on Feishu app credentials (such as `FEISHU_APP_ID` and `FEISHU_APP_SECRET`) securely injected by the host platform at runtime.
- **Runtime boundary**: If the host platform does not provide a Feishu connector or the user has not completed authorization, this skill should only fall back to "general advisory mode," not pretend it has already connected to Feishu resources.

### Recommended Permission Scope
To achieve "digital command center" capabilities, it is recommended that the associated Feishu app has the following minimum permission scope:

- **Messages**: Read and send group / private chat messages
- **Approvals**: Read approval instances and node status
- **Bitables**: Read and edit business table data
- **Calendar & Meetings**: Read and coordinate schedules and meeting information
- **Contacts / Organization Info**: Used for hierarchy judgment and communication tact adaptation

Note: The above is a **recommended permission scope**, not permissions self-applied by this skill. The actual access scope shall be determined by the host platform's connector and user authorization results, following the principle of least privilege.

### Pre-flight Check
Before executing any high-permission action, the agent should complete the following checks:

1. **Environment check**: If `FEISHU_APP_ID` is not detected or runtime authentication is not ready, fall back to advisory mode without attempting to call Feishu APIs.
2. **Permission check**: If the connector returns insufficient permissions, prompt the user to complete authorization, rather than blindly retrying.
3. **Context check**: If the task lacks necessary context (e.g., unknown target group, approval document, or table object), first ask for the missing key variables.
4. **Tact check**: For sensitive actions such as cross-department follow-ups, upward reports, or public reminders, first provide suggested copy or request confirmation before entering the execution chain.

### Data Handling
- This skill itself does not define any external data reporting endpoints.
- Summaries, minutes, follow-up suggestions, and action chain outputs should be completed within the current session and the host platform's private context.
- This skill should not forward Feishu data to third-party services without explanation.
- Data reading scope is strictly limited by the user's current permissions, the host platform's connector capabilities, and the task context.

### Least-Action Principle
This skill defaults to the following sequence:

**First identify → Then suggest → Then confirm → Then trigger**

This means:
- Prioritize problem identification over direct high-permission operations
- Prioritize draft copy generation over direct sending
- Prioritize risk and boundary warnings over creating the illusion of "completion"

The data in Feishu belongs to the organization, permissions belong to roles.  
This skill's responsibility is not to overstep and act on behalf of people, but to recompile fragmented collaboration into clearer, more tactful action chains.

---

## Access Model & Engineering Identity

### Attribute Description
- **Type:** Instruction-only Orchestrator
- **Dependencies:** Underlying API access depends on a trusted Feishu Connector provided by the host platform; this skill itself contains no network request code, installation scripts, or binary files
- **Credentials:** Runtime credentials are securely provided by the host platform (e.g., `FEISHU_APP_ID` / `FEISHU_APP_SECRET`); this skill does not persist any Feishu credentials

### Runtime Boundaries
- If the environment is not ready, this skill must fall back to "general advisory mode," not pretend it has already connected to Feishu resources
- If permissions are insufficient, this skill must prompt the user to complete authorization, not blindly retry
- If context is insufficient, this skill must first fill in the missing key variables before deciding whether to enter the execution chain

### Recommended Permission Layering
To achieve "digital command center" capabilities, it is recommended to understand permissions as two layers:

**Core (Default Safe Zone)**
- Read messages
- Read documents
- Read bitables
- Read calendar / meetings
- Generate summaries, drafts, suggestions

**Extended (Authorization Expansion Zone)**
- Send messages
- Edit bitables
- Coordinate schedules
- Trigger process-type write operations

Even when in Extended, the principle of double confirmation for sensitive actions still applies.

### Least-Action Principle
This skill defaults to the following sequence:

**First identify → Then suggest → Then authorize → Then execute**

This means:
- Prioritize outputting suggestions over direct execution
- Prioritize drafting content over direct sending
- Prioritize confirming boundaries over creating the illusion of "completion"

This skill's value lies not in overstepping and acting on behalf of people,  
but in recompiling fragmented collaboration into clearer, more tactful action chains.
