# ansible-go

Attempting to reproduce Ansible's APIs with a programming language (instead of
YAML), so you have more flexibility about how things get called.

Right now supports running commands on a single host at a time. The only
supported protocol is SSH'ing to a remote host and running some commands
(instead of, say, running mysql from your local machine to a remote host).

For the moment we are targeting Ubuntu Linux. If we want to support other
platforms, probably the way to implement this will be to copy the subclassing
relationship in Ansible, documented e.g. here:

```python
    A subclass may wish to override the following action methods:-
      - create_user()
      - remove_user()
      - modify_user()
      - ssh_key_gen()
      - ssh_key_fingerprint()
      - user_exists()

    All subclasses MUST define platform and distribution (which may be None).
```

But use interfaces for those as well, so:

```go
type UserImpl interface {
    Add(context.Context, ssh.Host, string, UserOpts)
    Remove(context.Context, ssh.Host, string)
    Modify(context.Context, ssh.Host, string, UserOpts)
    Exists(context.Context, ssh.Host, string) bool
    SSHKeyGen(context.Context, ssh.Host, string, SSHKeyGenOpts)
}
```

where `core.AddUser` switches the impl based on the `host.Platform`.
