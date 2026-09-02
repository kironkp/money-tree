release: python manage.py migrate --noinput
web: gunicorn moneytree.wsgi --log-file -
# Optional on Heroku (needs Postgres + Alpaca keys): the trading loop.
# worker: python manage.py run_agent --mode paper
