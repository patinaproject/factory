## This event

- Event: {event_type}, action {action}
- Repository: {repository.full_name}
- Sender: {sender.login}, GitHub user id {sender.id}
- Pull request: #{pull_request.number} on branch {pull_request.head.ref}, {pull_request.html_url}, merged {pull_request.merged}
- Issue: #{issue.number}, {issue.html_url}; it is a pull request when this is a URL: {issue.pull_request.html_url}
- Comment: {comment.html_url}
- Comment body: {comment.body}
- Review: {review.state}, {review.html_url}
- Review body: {review.body}
- Workflow run: {workflow_run.name} concluded {workflow_run.conclusion} on branch {workflow_run.head_branch}, {workflow_run.html_url}
- Check run: {check_run.name} concluded {check_run.conclusion} on branch {check_run.check_suite.head_branch}, {check_run.html_url}

A field still shown as a dotted name in braces is absent from this payload. The
full payload, truncated to 4000 characters:

{__raw__}
