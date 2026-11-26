#!/usr/bin/env bash
# fixture caution scanner: emits a reminder when the written text claims resolution
t=$(cat)
if printf '%s' "$t" | grep -qiE 'resolved|root cause identified|breakthrough'; then
  echo "<system-reminder>"; echo "CAUTION (fixture): claims of resolution were false 3 of 4 times on this topic — show the evidence first."; echo "</system-reminder>"
fi
exit 0
