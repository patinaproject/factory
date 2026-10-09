export type ReviewStatus = "open" | "resolved" | "superseded";
export interface PullRequestHead {
  readonly repository: string;
  readonly branch: string;
  readonly number: number;
  readonly head: string;
  readonly isDraft: boolean;
}
export interface ReviewFinding {
  readonly id: string;
  readonly kind: "hard" | "soft" | "smell" | "scope-creep";
}
export interface ReviewRecord {
  readonly path: string;
  readonly head: string;
  readonly status: ReviewStatus;
  readonly standards: string;
  readonly spec: string;
  readonly findings: readonly ReviewFinding[];
  readonly resolutions: readonly string[];
  readonly blockingFindings: number;
  readonly dismissedFindings: number;
}
export interface ForgeAdapter {
  readonly name: "gh" | "origin";
  readPullRequest(number: number, repository?: string): PullRequestHead;
  markReady(number: number, repository?: string, expectedHead?: string): void;
}
