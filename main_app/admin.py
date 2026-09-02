from django.contrib import admin

from . import models

for model in (
    models.Instrument, models.Bar, models.MarketSession, models.AgentConfig, models.Account,
    models.Position, models.Order, models.Fill, models.Trade, models.EquitySnapshot,
    models.Signal, models.RiskEvent, models.AgentRun, models.Strategy, models.Experiment,
    models.BacktestRun, models.BacktestTrade, models.JournalEntry, models.ApiUsage, models.FeedEvent,
    models.SignupInvite,
):
    admin.site.register(model)
