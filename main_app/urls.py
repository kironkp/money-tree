from django.urls import path

from . import views

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('partials/dashboard/', views.dashboard_panels, name='dashboard-panels'),
    path('partials/agent-log/', views.agent_log, name='agent-log'),
    path('partials/status/', views.status_strip, name='status-strip'),
    path('alerts/<int:pk>/ack/', views.alert_ack, name='alert-ack'),
    path('cards/<int:pk>/<str:verdict>/', views.card_decide, name='card-decide'),
    path('api/equity/<str:mode>/', views.api_equity, name='api-equity'),
    path('agent/start/', views.agent_start, name='agent-start'),
    path('agent/stop/', views.agent_stop, name='agent-stop'),
    path('agent/kill/', views.kill_switch, name='kill-switch'),
    path('agent/flatten/', views.flatten_now, name='flatten-now'),

    path('positions/', views.positions, name='positions'),
    path('orders/', views.orders, name='orders'),
    path('trades/', views.trades, name='trades'),
    path('trades/export.csv', views.trades_csv, name='trades-csv'),
    path('signals/', views.signals, name='signals'),
    path('risk/', views.risk_events, name='risk-events'),

    path('strategies/', views.strategy_list, name='strategy-list'),
    path('strategies/create-missing/', views.strategy_create_missing, name='strategy-create-missing'),
    path('strategies/<str:market>/portfolio-backtest/', views.portfolio_backtest, name='portfolio-backtest'),
    path('strategies/<str:market>/<str:key>/', views.strategy_detail, name='strategy-detail'),
    path('strategies/<str:market>/<str:key>/backtest/', views.strategy_backtest, name='strategy-backtest'),

    path('backtests/', views.backtest_list, name='backtest-list'),
    path('backtests/compare/', views.backtest_compare, name='backtest-compare'),
    path('backtests/<int:pk>/', views.backtest_detail, name='backtest-detail'),
    path('backtests/<int:pk>/promote/', views.backtest_promote, name='backtest-promote'),
    path('backtests/<int:pk>/delete/', views.backtest_delete, name='backtest-delete'),
    path('api/backtests/<int:pk>/equity/', views.api_backtest_equity, name='api-backtest-equity'),

    path('experiments/', views.experiment_list, name='experiment-list'),
    path('experiments/<int:pk>/', views.experiment_detail, name='experiment-detail'),
    path('experiments/<int:pk>/progress/', views.experiment_progress, name='experiment-progress'),
    path('experiments/<int:pk>/promote/', views.experiment_promote, name='experiment-promote'),
    path('experiments/<int:pk>/stop/', views.experiment_stop, name='experiment-stop'),
    path('experiments/<int:pk>/delete/', views.experiment_delete, name='experiment-delete'),
    path('replay/', views.replay, name='replay'),

    path('data/', views.data_index, name='data-index'),
    path('data/add/', views.instrument_add, name='instrument-add'),
    path('data/sync/', views.data_sync, name='data-sync'),
    path('data/sync/log/', views.sync_log, name='sync-log'),
    path('data/<str:slug>/', views.instrument_chart, name='instrument-chart'),
    path('data/<str:slug>/toggle/', views.instrument_toggle, name='instrument-toggle'),
    path('data/<str:slug>/delete/', views.instrument_delete, name='instrument-delete'),
    path('api/bars/<str:slug>/', views.api_bars, name='api-bars'),

    path('journal/', views.journal_list, name='journal-list'),
    path('journal/add/', views.journal_add, name='journal-add'),
    path('journal/eod/', views.journal_eod_now, name='journal-eod'),
    path('journal/coach/', views.journal_coach_now, name='journal-coach'),
    path('journal/<int:pk>/run/<int:n>/', views.journal_run_proposal, name='journal-run-proposal'),
    path('journal/<int:pk>/delete/', views.journal_delete, name='journal-delete'),

    path('settings/', views.settings_view, name='settings'),
    path('settings/mode/', views.set_mode, name='set-mode'),
    path('settings/reset/', views.reset_account, name='reset-account'),

    path('settings/people/', views.people, name='people'),
    path('feed/', views.feed_page, name='feed'),
    path('api/feed/', views.api_feed, name='api-feed'),
    path('api/spend/', views.api_spend_ingest, name='api-spend-ingest'),
    path('api/spend/report/', views.api_spend, name='api-spend-report'),
    path('spend/', views.spend_page, name='spend'),
]
