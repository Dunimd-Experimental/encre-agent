Automation mode runs repetitive work on a schedule and pushes results to you. Perfect for "check this every day" chores like reports, monitoring, and scans.

## Entering automation

Switch to **Automation** mode at the top. The automation page has two tabs:

- **Configured**: all the tasks you have created
- **Execution history**: records and results of every run

## Creating a task

Click **New task** and create from a template or from scratch:

**Built-in templates**

| Template | Purpose |
| --- | --- |
| Daily AI News Briefing | Aggregates AI news every day |
| Weekly Brand Sentiment Report | Weekly coverage of brand sentiment |
| Weekly Competitor Tracking | Tracks competitor updates |
| Stock Price Monitoring | Monitors prices and alerts on swings |
| Security Vulnerability Scan | Periodic security scanning |
| Bug Scan on Commits | Scans code commits for potential bugs |
| Test Coverage Filling | Analyzes and improves test coverage |
| Daily Change Summary | Summarizes the day's project changes |

Or choose **Custom create** to fully define the task with your own prompt.

## Task fields

| Field | Description |
| --- | --- |
| Task name | A recognizable name |
| Schedule | How often: daily / weekly / hourly / weekdays / custom / once / every minute |
| Execution model | The model that runs this task |
| Enable push | Toggle to push results to the **target gateways** |
| Target gateways | Choose configured gateway channels (e.g. Telegram, email) |
| What to do | Tell the agent what to do |

![Creating an automation task](/screenshots/automation-create.png)

## Managing tasks

In the **Configured** list, each task offers:

- **Run now**: execute once immediately (no need to wait for the schedule)
- **Enable / Pause**: paused tasks won't trigger, but keep their config
- **Delete**: remove the task
- **View history**: open this task's execution records

## Execution history

The **Execution history** tab lets you:

- Filter by result (all / success / failed)
- Filter by date range (from / to)
- **Export** execution records
- See each run's status: running / paused / waiting / success / failed
- Click a run and go **back to chat** to review the agent's full execution

![Execution history](/screenshots/automation-history.png)

## FAQ

**Q: What if a run fails?**
A: Open that run from history and "return to chat" to see the cause. Common error codes: `-1` means a Bash timeout, `-2` means Docker is not installed.

**Q: Can automation work without a gateway?**
A: Yes. Pushing is optional — tasks run fine without it; results are just not sent to a chat tool. See [Gateway](/en/desktop/gateway) to configure one.

**Q: Will tasks consume many resources?**
A: Tasks only run when scheduled and are governed by the permission system. It's a good idea to use a separate model for automation vs. chat.