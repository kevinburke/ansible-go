# ansible-go

Attempting to reproduce Ansible's APIs with a programming language (instead of
YAML), so you have more flexibility about how things get called.

Right now supports running commands on a single host at a time. The only
supported protocol is SSH'ing to a remote host and running some commands
(instead of, say, running mysql from your local machine to a remote host).
