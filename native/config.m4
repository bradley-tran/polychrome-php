PHP_ARG_ENABLE([polychrome], [whether to build Polychrome],
  [AS_HELP_STRING([--enable-polychrome], [Build the Polychrome FastCGI SAPI])], [no], [no])

if test "$PHP_POLYCHROME" != "no"; then
  AS_VAR_IF([PHP_THREAD_SAFETY], [yes], [AC_MSG_ERROR([Polychrome requires NTS PHP])])
  AC_ARG_VAR([POLYCHROME_RUST_LIB], [Absolute path to libpolychrome_php.a])
  AS_VAR_IF([POLYCHROME_RUST_LIB], [], [AC_MSG_ERROR([POLYCHROME_RUST_LIB is required])])
  PHP_SELECT_SAPI([polychrome], [program], [sapi.c fastcgi_bridge.c], [-DZEND_ENABLE_STATIC_TSRMLS_CACHE=1])
  SAPI_POLYCHROME_PATH=sapi/polychrome/polychrome
  PHP_SUBST([SAPI_POLYCHROME_PATH])
  PHP_SUBST([POLYCHROME_RUST_LIB])
  PHP_ADD_MAKEFILE_FRAGMENT([$abs_srcdir/sapi/polychrome/Makefile.frag])
fi
