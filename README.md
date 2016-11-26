# go-ansible

Attempting to reproduce Ansible's APIs with a programming language, so you have
more flexibility.

Right now supports running commands on a single host at a time. The only
supported protocol is SSH'ing to a remote host and running some commands
(instead of, say, running mysql from your local machine to a remote host).
