use crate::config::Config;
use std::{
    collections::BTreeMap,
    ffi::{CStr, CString},
    fs, io,
    net::TcpListener,
    os::{
        fd::{AsRawFd, FromRawFd, OwnedFd, RawFd},
        unix::{
            ffi::OsStrExt,
            fs::{MetadataExt, PermissionsExt},
            net::UnixListener,
        },
    },
    path::PathBuf,
    ptr,
    time::{Duration, Instant},
};

unsafe extern "C" {
    fn poly_php_version() -> *const libc::c_char;
    fn poly_initialize(
        root: *const libc::c_char,
        ini: *const libc::c_char,
        defines: *const *const libc::c_char,
        count: usize,
        preload: *const libc::c_char,
    ) -> libc::c_int;
    fn poly_serve_one(listener: libc::c_int, events: libc::c_int) -> libc::c_int;
    fn poly_shutdown();
}

pub fn php_version() -> &'static str {
    unsafe {
        CStr::from_ptr(poly_php_version())
            .to_str()
            .unwrap_or("unknown")
    }
}

struct Listener {
    fd: OwnedFd,
    socket: Option<(PathBuf, u64, u64)>,
}

impl Listener {
    fn bind(endpoint: &str) -> io::Result<Self> {
        if let Some(path) = endpoint.strip_prefix("unix:") {
            // Never unlink an existing socket: it may belong to another server.
            let socket = UnixListener::bind(path)?;
            let metadata = fs::symlink_metadata(path)?;
            let result = Self {
                fd: socket.into(),
                socket: Some((path.into(), metadata.dev(), metadata.ino())),
            };
            fs::set_permissions(path, fs::Permissions::from_mode(0o660))?;
            Ok(result)
        } else {
            Ok(Self {
                fd: TcpListener::bind(endpoint)?.into(),
                socket: None,
            })
        }
    }
}

impl Drop for Listener {
    fn drop(&mut self) {
        if let Some((path, dev, ino)) = &self.socket
            && fs::symlink_metadata(path).is_ok_and(|m| m.dev() == *dev && m.ino() == *ino)
        {
            let _ = fs::remove_file(path);
        }
    }
}

struct Worker {
    events: OwnedFd,
    started: Option<u64>,
    killed: bool,
    born: Instant,
}

fn monotonic_ns() -> u64 {
    unsafe {
        let mut time = std::mem::zeroed();
        libc::clock_gettime(libc::CLOCK_MONOTONIC, &mut time);
        time.tv_sec as u64 * 1_000_000_000 + time.tv_nsec as u64
    }
}

fn signal_fd() -> io::Result<OwnedFd> {
    unsafe {
        let mut mask = std::mem::zeroed();
        libc::sigemptyset(&mut mask);
        for signal in [libc::SIGCHLD, libc::SIGTERM, libc::SIGINT, libc::SIGUSR1] {
            libc::sigaddset(&mut mask, signal);
        }
        if libc::sigprocmask(libc::SIG_BLOCK, &mask, ptr::null_mut()) < 0 {
            return Err(io::Error::last_os_error());
        }
        let fd = libc::signalfd(-1, &mask, libc::SFD_CLOEXEC | libc::SFD_NONBLOCK);
        if fd < 0 {
            Err(io::Error::last_os_error())
        } else {
            Ok(OwnedFd::from_raw_fd(fd))
        }
    }
}

fn spawn(
    listener: RawFd,
    signals: RawFd,
    workers: &BTreeMap<libc::pid_t, Worker>,
) -> io::Result<(libc::pid_t, Worker)> {
    unsafe {
        let mut pair = [-1; 2];
        if libc::socketpair(
            libc::AF_UNIX,
            libc::SOCK_DGRAM | libc::SOCK_CLOEXEC | libc::SOCK_NONBLOCK,
            0,
            pair.as_mut_ptr(),
        ) < 0
        {
            return Err(io::Error::last_os_error());
        }
        let parent = OwnedFd::from_raw_fd(pair[0]);
        let child = OwnedFd::from_raw_fd(pair[1]);
        let expected_parent = libc::getpid();
        let pid = libc::fork();
        if pid < 0 {
            return Err(io::Error::last_os_error());
        }
        if pid == 0 {
            libc::close(parent.as_raw_fd());
            libc::close(signals);
            for worker in workers.values() {
                libc::close(worker.events.as_raw_fd());
            }
            // If the zygote dies, do not leave children holding the listener.
            libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGKILL);
            if libc::getppid() != expected_parent {
                libc::_exit(1);
            }
            // Signal handlers and unmasking are installed in C before accept.
            let status = poly_serve_one(listener, child.as_raw_fd());
            // Request shutdown already ran. Never run inherited Rust or PHP
            // module destructors in a disposable child.
            libc::_exit(status);
        }
        drop(child);
        Ok((
            pid,
            Worker {
                events: parent,
                started: None,
                killed: false,
                born: Instant::now(),
            },
        ))
    }
}

fn supervise(config: &Config, listener: &Listener, signals: &OwnedFd) -> io::Result<()> {
    let mut workers = BTreeMap::<libc::pid_t, Worker>::new();
    let mut draining = None;
    let mut retry_at = Instant::now();
    let mut failures = 0u32;
    let mut ready = false;
    let mut fatal = None;
    loop {
        if draining.is_none() && Instant::now() >= retry_at {
            while workers.len() < config.workers {
                match spawn(listener.fd.as_raw_fd(), signals.as_raw_fd(), &workers) {
                    Ok((pid, worker)) => {
                        workers.insert(pid, worker);
                    }
                    Err(error) => {
                        failures = failures.saturating_add(1);
                        retry_at = Instant::now()
                            + Duration::from_millis((100u64 << failures.min(6)).min(5000));
                        eprintln!("polychrome: spawn failed: {error}; backing off");
                        break;
                    }
                }
            }
            if !ready && workers.len() == config.workers {
                eprintln!(
                    "polychrome: ready root={} listen={} workers={} timeout={}s preload={}",
                    config.root.display(),
                    config.listen,
                    config.workers,
                    config.timeout.as_secs(),
                    config.preload
                );
                ready = true;
            }
        }

        let mut fds = Vec::with_capacity(workers.len() + 1);
        fds.push(libc::pollfd {
            fd: signals.as_raw_fd(),
            events: libc::POLLIN,
            revents: 0,
        });
        for worker in workers.values() {
            fds.push(libc::pollfd {
                fd: worker.events.as_raw_fd(),
                events: libc::POLLIN,
                revents: 0,
            });
        }
        let poll_result = unsafe { libc::poll(fds.as_mut_ptr(), fds.len() as _, 100) };
        if poll_result < 0 && io::Error::last_os_error().kind() != io::ErrorKind::Interrupted {
            fatal = Some(io::Error::last_os_error());
            draining = Some(Instant::now());
        }

        // Read accept timestamps before processing shutdown, so active work is
        // accounted for even when SIGCHLD/SIGTERM arrives in the same poll.
        for worker in workers.values_mut() {
            let mut stamp = 0u64;
            while unsafe {
                libc::recv(
                    worker.events.as_raw_fd(),
                    (&mut stamp as *mut u64).cast(),
                    8,
                    libc::MSG_DONTWAIT,
                )
            } == 8
            {
                worker.started.get_or_insert(stamp);
            }
        }
        loop {
            let mut info: libc::signalfd_siginfo = unsafe { std::mem::zeroed() };
            let size = std::mem::size_of_val(&info);
            if unsafe {
                libc::read(
                    signals.as_raw_fd(),
                    (&mut info as *mut libc::signalfd_siginfo).cast(),
                    size,
                )
            } != size as isize
            {
                break;
            }
            if matches!(info.ssi_signo as i32, libc::SIGTERM | libc::SIGINT) {
                if draining.is_some() {
                    draining = Some(Instant::now());
                } else {
                    eprintln!("polychrome: draining requests (30s maximum)");
                    draining = Some(Instant::now() + Duration::from_secs(30));
                    for &pid in workers.keys() {
                        unsafe {
                            libc::kill(pid, libc::SIGUSR1);
                        }
                    }
                }
            }
        }

        let now = monotonic_ns();
        for (&pid, worker) in &mut workers {
            let expired = worker
                .started
                .is_some_and(|start| now.saturating_sub(start) >= config.timeout.as_nanos() as u64);
            let drain_expired = draining.is_some_and(|deadline| Instant::now() >= deadline);
            if !worker.killed && (expired || drain_expired) {
                eprintln!(
                    "polychrome: terminating child pid={pid} reason={}",
                    if expired {
                        "request-timeout"
                    } else {
                        "shutdown-deadline"
                    }
                );
                unsafe {
                    libc::kill(pid, libc::SIGKILL);
                }
                worker.killed = true;
            }
        }
        loop {
            let mut status = 0;
            let pid = unsafe { libc::waitpid(-1, &mut status, libc::WNOHANG) };
            if pid <= 0 {
                break;
            }
            if let Some(worker) = workers.remove(&pid) {
                let abnormal = !libc::WIFEXITED(status) || libc::WEXITSTATUS(status) != 0;
                if abnormal {
                    eprintln!("polychrome: child exited pid={pid} status={status}");
                }
                if abnormal
                    && worker.started.is_none()
                    && worker.born.elapsed() < Duration::from_secs(1)
                {
                    failures = failures.saturating_add(1);
                    retry_at = Instant::now()
                        + Duration::from_millis((100u64 << failures.min(6)).min(5000));
                } else {
                    failures = 0;
                }
            }
        }
        if draining.is_some() && workers.is_empty() {
            break;
        }
    }
    fatal.map_or(Ok(()), Err)
}

pub fn run(config: Config) -> Result<(), String> {
    if !cfg!(target_os = "linux") {
        return Err("v0.1 requires Linux".into());
    }
    let root = CString::new(config.root.as_os_str().as_bytes()).map_err(|_| "root contains NUL")?;
    let ini = config
        .ini
        .as_ref()
        .map(|p| CString::new(p.as_os_str().as_bytes()))
        .transpose()
        .map_err(|_| "INI path contains NUL")?;
    let defines: Vec<_> = config
        .defines
        .iter()
        .map(|s| CString::new(s.as_str()).unwrap())
        .collect();
    let pointers: Vec<_> = defines.iter().map(|s| s.as_ptr()).collect();
    let helper = if config.preload {
        let executable = std::env::current_exe().map_err(|e| e.to_string())?;
        let helper = executable
            .parent()
            .unwrap()
            .join("../share/polychrome/preload.php")
            .canonicalize()
            .map_err(|e| format!("cannot locate installed preload helper: {e}"))?;
        Some(CString::new(helper.as_os_str().as_bytes()).map_err(|_| "preload path contains NUL")?)
    } else {
        None
    };

    std::env::set_current_dir(&config.root).map_err(|e| e.to_string())?;
    let signals = signal_fd().map_err(|e| format!("signals: {e}"))?;
    let initialized = unsafe {
        poly_initialize(
            root.as_ptr(),
            ini.as_ref().map_or(ptr::null(), |s| s.as_ptr()),
            pointers.as_ptr(),
            pointers.len(),
            helper.as_ref().map_or(ptr::null(), |s| s.as_ptr()),
        )
    };
    if initialized != 0 {
        return Err("PHP initialization failed; see diagnostics above".into());
    }
    let result = (|| {
        // Extensions that start threads cannot safely participate in this
        // fork-without-exec architecture, even with an NTS PHP build.
        let tasks = fs::read_dir("/proc/self/task")
            .map_err(|e| e.to_string())?
            .count();
        if tasks != 1 {
            return Err(
                "an extension started threads in the zygote; disable it before forking".into(),
            );
        }
        let listener =
            Listener::bind(&config.listen).map_err(|e| format!("listen {}: {e}", config.listen))?;
        supervise(&config, &listener, &signals).map_err(|e| e.to_string())
    })();
    unsafe {
        poly_shutdown();
    }
    result
}
