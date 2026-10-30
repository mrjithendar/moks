kubectl --context mk4 -n moks exec -it deploy/moks-api -c api -- \
  moks create-user team@jithendar.com --password-prompt --grant admin