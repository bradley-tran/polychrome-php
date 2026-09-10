# Polychrome PHP

A high performance PHP runtime inspired by Android's Zygote process architecture.

Polychrome consist of a process manager (like RoadRunner) and a custom PHP SAPI (like FrankenPHP); but unlike RoadRunner, it is designed to work without config and unlike FrankenPHP, the forked PHP processes are non-persistent.

Polychrome does not aim to be as fast as either. Its goal is to improve performance for codebases that are not ready for worker mode / persistent process, in particular the current Drupal codebase.
