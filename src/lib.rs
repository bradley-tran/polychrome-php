mod config;
#[cfg(not(test))]
mod manager;

#[cfg(not(test))]
#[unsafe(no_mangle)]
/// Entry point called by the native SAPI executable.
pub extern "C" fn polychrome_main() -> libc::c_int {
    // No Rust callbacks occur while PHP is executing. PHP bailouts are caught
    // inside the C shim; Rust must never be unwound by Zend's longjmp.
    match config::parse(std::env::args_os().skip(1)) {
        Ok(config::Command::Help) => {
            println!("{}", config::HELP);
            0
        }
        Ok(config::Command::Version) => {
            println!(
                "Polychrome {} (PHP {})",
                env!("CARGO_PKG_VERSION"),
                manager::php_version()
            );
            0
        }
        Ok(config::Command::Serve(config)) => match manager::run(config) {
            Ok(()) => 0,
            Err(error) => {
                eprintln!("polychrome: {error}");
                1
            }
        },
        Err(error) => {
            eprintln!("polychrome: {error}\nTry --help for usage.");
            2
        }
    }
}
