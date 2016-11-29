package mysql

import (
	"context"
	"errors"
	"fmt"
	"io"
	"strings"

	"github.com/kevinburke/ansible-go/core"
	"github.com/kevinburke/ansible-go/shell"
	"github.com/kevinburke/ansible-go/ssh"
)

type DumpConfig struct {
	ExecConfig
	SingleTransaction bool
}

type Privilege struct {
	// Database name or "*" for all
	Database string
	// Database name or "*" for all
	Table string
	// Array of privileges, []string{"ALL"} for all
	Privileges []string
}

func (p Privilege) String() string {
	db, table := "*", "*"
	if p.Table != "" {
		table = p.Table
	}
	if p.Database != "" {
		db = p.Database
	}
	return fmt.Sprintf("GRANT %s ON %s . %s", strings.Join(p.Privileges, ","), db, table)
}

type CreateConfig struct {
	Host      string
	Password  string
	Privilege Privilege
	Port      string
}

func CreateUser(ctx context.Context, host ssh.Host, name string, cfg CreateConfig) error {
	if cfg.Host == "" {
		cfg.Host = "localhost"
	}
	createCmd := fmt.Sprintf("CREATE USER IF NOT EXISTS %s@%s", shell.Escape(name), shell.Escape(cfg.Host))
	if cfg.Password != "" {
		createCmd = createCmd + fmt.Sprintf(" IDENTIFIED BY '%s'", shell.Escape(cfg.Password))
	}
	grantCmd := fmt.Sprintf("%s TO %s@%s", cfg.Privilege.String(), name, cfg.Host)
	args := []string{"--execute", strings.Join([]string{createCmd, grantCmd}, "; ")}
	return core.RunCommand(ctx, "mysql", shell.Escape(args...))
}

// DumpWriter runs mysqldump on the remote host and writes the contents to
// target.
func DumpWriter(ctx context.Context, host ssh.Host, dbName string, target io.Writer, cfg DumpConfig) error {
	return errors.New("need multiplex SSH")
	//if cfg.Port == "" {
	//cfg.Port = "3306"
	//}
	//if cfg.Host == "" {
	//cfg.Host = "localhost"
	//}
	//args := []string{
	//"--compress",
	//"--tz-utc", "--dump-date",
	//}
	//if cfg.SingleTransaction {
	//args = append(args, "--single-transaction")
	//}
	//if cfg.User != "" {
	//args = append(args, "--user", cfg.User)
	//}
	//if cfg.Password != "" {
	//args = append(args, fmt.Sprintf("--password=%s", cfg.Password))
	//}
	//args = append(args, "--databases", dbName)
	//return ssh.RunCommandStdout(ctx, host, target, "mysqldump", shell.Escape(args...))
}

type ExecConfig struct {
	User     string
	Password string
	Host     string
	Port     string
}

type RunConfig struct {
	ExecConfig
	// Name of the database to run commands on
	Database string
}

// RunCommands pipes the given cmds to mysql.
func RunCommands(ctx context.Context, cmds io.Reader, cfg RunConfig) error {
	if cfg.Port == "" {
		cfg.Port = "3306"
	}
	if cfg.Host == "" {
		cfg.Host = "localhost"
	}
	args := []string{"--port", cfg.Port, "--host", cfg.Host}
	if cfg.User != "" {
		args = append(args, "--user", cfg.User)
	}
	if cfg.Password != "" {
		args = append(args, fmt.Sprintf("--password=%s", cfg.Password))
	}
	if cfg.Database != "" {
		args = append(args, cfg.Database)
	}
	return core.RunCommandStdin(ctx, cmds, "mysql", shell.Escape(args...))
}
