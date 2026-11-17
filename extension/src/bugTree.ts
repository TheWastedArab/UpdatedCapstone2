import * as vscode from 'vscode';
import { Api } from './api';

export interface BugSummary {
  id: number;
  title: string;
  status: string;
  severity: string;
  assignee: { display_name: string } | null;
}

const STATUSES = ['Open', 'In Progress', 'Resolved', 'Verified', 'Closed'];

type Node = { kind: 'status'; status: string; bugs: BugSummary[] } | { kind: 'bug'; bug: BugSummary };

/** Sidebar tree: the current project's bugs grouped by lifecycle status. */
export class BugTree implements vscode.TreeDataProvider<Node> {
  private readonly changed = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this.changed.event;
  private bugs: BugSummary[] = [];
  private total = 0;

  constructor(private readonly api: Api, private readonly projectId: () => number | undefined) {}

  async refresh(): Promise<void> {
    const projectId = this.projectId();
    this.bugs = [];
    this.total = 0;
    if (this.api.signedIn && projectId !== undefined) {
      try {
        const page = await this.api.request<{ items: BugSummary[]; total: number }>('GET', `/projects/${projectId}/bugs`, {
          query: { page_size: 100 },
        });
        this.bugs = page.items;
        this.total = page.total;
      } catch {
        // The tree stays empty; the panel shows the actual error to the user.
      }
    }
    this.changed.fire();
  }

  getChildren(node?: Node): Node[] {
    if (!node) {
      return STATUSES.map((status) => ({ kind: 'status' as const, status, bugs: this.bugs.filter((b) => b.status === status) })).filter(
        (group) => group.bugs.length > 0,
      );
    }
    return node.kind === 'status' ? node.bugs.map((bug) => ({ kind: 'bug' as const, bug })) : [];
  }

  getTreeItem(node: Node): vscode.TreeItem {
    if (node.kind === 'status') {
      const item = new vscode.TreeItem(node.status, node.status === 'Closed' ? vscode.TreeItemCollapsibleState.Collapsed : vscode.TreeItemCollapsibleState.Expanded);
      item.description = String(node.bugs.length);
      return item;
    }
    const { bug } = node;
    const item = new vscode.TreeItem(`#${bug.id} ${bug.title}`);
    item.description = bug.severity;
    item.tooltip = `${bug.title}\nSeverity: ${bug.severity}\nStatus: ${bug.status}\nAssignee: ${bug.assignee?.display_name ?? 'Unassigned'}`;
    item.accessibilityInformation = { label: `Bug ${bug.id}, ${bug.title}, severity ${bug.severity}, status ${bug.status}` };
    item.iconPath = new vscode.ThemeIcon('bug');
    item.command = { command: 'bugTracker.open', title: 'Open bug', arguments: [{ view: 'bug', id: bug.id }] };
    return item;
  }

  get truncated(): boolean {
    return this.total > this.bugs.length;
  }
}
