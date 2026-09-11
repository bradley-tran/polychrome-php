use std::{ffi::OsString, path::PathBuf, time::Duration};

#[cfg_attr(test, allow(dead_code))]
pub const HELP: &str = "Polychrome — one-request-per-process PHP FastCGI server

Usage: polychrome [OPTIONS]

  --root DIR                 Application root (default: current directory)
  --listen ENDPOINT          IP:port or unix:/absolute/path.sock (127.0.0.1:9000)
  --workers N                Concurrent children (default: available CPUs)
  --request-timeout SECONDS  Wall-clock connection/request deadline (30)
  --no-preload               Disable automatic code preloading
  -c FILE                    PHP configuration file or directory
  -d KEY=VALUE               Override a PHP INI setting; repeatable
  --help                     Show this help
  --version                  Show Polychrome and embedded PHP versions

Runs in the foreground. SIGTERM/SIGINT drain requests for up to 30 seconds.
Restart after changing preloaded code. HTTP routing and TLS need a web server.";

#[derive(Debug)]
pub enum Command {
    Help,
    Version,
    Serve(Config),
}

#[derive(Debug)]
pub struct Config {
    pub root: PathBuf,
    pub listen: String,
    pub workers: usize,
    pub timeout: Duration,
    pub preload: bool,
    pub ini: Option<PathBuf>,
    pub defines: Vec<String>,
}

pub fn parse(args: impl IntoIterator<Item = OsString>) -> Result<Command, String> {
    let mut config = Config {
        root: PathBuf::from("."),
        listen: "127.0.0.1:9000".into(),
        workers: std::thread::available_parallelism().map_or(1, usize::from),
        timeout: Duration::from_secs(30),
        preload: true,
        ini: None,
        defines: Vec::new(),
    };
    let mut args = args.into_iter();
    while let Some(arg) = args.next() {
        let arg = arg.to_str().ok_or("options must be valid UTF-8")?;
        if matches!(arg, "--help" | "-h") {
            return Ok(Command::Help);
        }
        if matches!(arg, "--version" | "-v") {
            return Ok(Command::Version);
        }
        if arg == "--no-preload" {
            config.preload = false;
            continue;
        }
        if !matches!(
            arg,
            "--root" | "--listen" | "--workers" | "--request-timeout" | "-c" | "-d"
        ) {
            return Err(format!("unknown option: {arg}"));
        }
        let value = args
            .next()
            .ok_or_else(|| format!("missing value for {arg}"))?;
        match arg {
            "--root" => config.root = value.into(),
            "-c" => config.ini = Some(value.into()),
            _ => {
                let value = value
                    .into_string()
                    .map_err(|_| format!("invalid UTF-8 value for {arg}"))?;
                match arg {
                    "--listen" => {
                        if let Some(path) = value.strip_prefix("unix:") {
                            if !std::path::Path::new(path).is_absolute() {
                                return Err("Unix socket path must be absolute".into());
                            }
                        } else {
                            let address = value.parse::<std::net::SocketAddr>().map_err(
                                |_| "--listen requires an IP:port or unix:/absolute/path",
                            )?;
                            if address.port() == 0 {
                                return Err("--listen port must be nonzero".into());
                            }
                        }
                        config.listen = value;
                    }
                    "--workers" => {
                        config.workers = value
                            .parse()
                            .ok()
                            .filter(|n| (1..=4096).contains(n))
                            .ok_or("--workers must be between 1 and 4096")?;
                    }
                    "--request-timeout" => {
                        let seconds = value
                            .parse::<u64>()
                            .ok()
                            .filter(|n| (1..=86400).contains(n))
                            .ok_or("--request-timeout must be between 1 and 86400 seconds")?;
                        config.timeout = Duration::from_secs(seconds);
                    }
                    "-d" => {
                        let (key, _) = value.split_once('=').ok_or("-d requires KEY=VALUE")?;
                        if key.is_empty()
                            || !key
                                .bytes()
                                .all(|c| c.is_ascii_alphanumeric() || matches!(c, b'.' | b'_'))
                            || value.contains(['\n', '\r', '\0'])
                        {
                            return Err("invalid PHP INI override".into());
                        }
                        if key.eq_ignore_ascii_case("opcache.preload")
                            || key.eq_ignore_ascii_case("opcache.preload_user")
                        {
                            return Err("Polychrome manages opcache.preload and opcache.preload_user; use --no-preload to disable it".into());
                        }
                        config.defines.push(value);
                    }
                    _ => unreachable!(),
                }
            }
        }
    }
    config.root = config
        .root
        .canonicalize()
        .map_err(|e| format!("application root: {e}"))?;
    if !config.root.is_dir() {
        return Err("application root must be a directory".into());
    }
    if let Some(path) = config.ini.as_mut() {
        *path = path
            .canonicalize()
            .map_err(|e| format!("PHP INI path: {e}"))?;
    }
    Ok(Command::Serve(config))
}

#[cfg(test)]
mod tests {
    use super::*;
    fn command(args: &[&str]) -> Result<Command, String> {
        parse(args.iter().map(OsString::from))
    }

    #[test]
    fn rejects_invalid_limits_and_reserved_settings() {
        for args in [
            vec!["--workers", "0"],
            vec!["--request-timeout", "-1"],
            vec!["--workers"],
            vec!["-d", "opcache.preload=app.php"],
            vec!["-d", "x=1\nother=2"],
            vec!["--listen", "unix:relative.sock"],
            vec!["--listen", "localhost:9000"],
        ] {
            assert!(command(&args).is_err(), "accepted {args:?}");
        }
    }

    #[test]
    fn accepts_explicit_runtime_options() {
        let Command::Serve(config) = command(&[
            "--workers",
            "2",
            "--listen",
            "[::1]:9010",
            "--no-preload",
            "-d",
            "memory_limit=256M",
        ])
        .unwrap() else {
            panic!()
        };
        assert_eq!(config.workers, 2);
        assert!(!config.preload);
        assert_eq!(config.defines, ["memory_limit=256M"]);
        assert!(config.root.is_absolute());
    }
}
