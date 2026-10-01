"""Google Flights lookups via the `fli` library (reverse-engineered API, no key)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional, Protocol

from .config import Config
from .store import Watch

log = logging.getLogger("flightwatch.search")


@dataclass
class Offer:
    price: float
    currency: str
    travel_date: str
    return_date: Optional[str] = None
    airline: Optional[str] = None
    flight_no: Optional[str] = None
    depart_time: Optional[str] = None
    stops: Optional[int] = None
    origin: Optional[str] = None
    destination: Optional[str] = None


class Searcher(Protocol):
    def cheapest(self, watch: Watch, start: date, end: date) -> Optional[Offer]: ...


class FliSearcher:
    """Cheapest date in the window (date grid), then that day's cheapest flight."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def _locale(self) -> dict:
        return {
            "currency": self.config.currency,
            "language": self.config.language,
            "country": self.config.country,
        }

    def cheapest(self, watch: Watch, start: date, end: date) -> Optional[Offer]:
        from fli.models import (Airport, DateSearchFilters, FlightSegment, MaxStops,
                                PassengerInfo, SeatType, TripType)
        from fli.search import SearchDates

        origins = [[getattr(Airport, c), 0] for c in watch.origin_codes]
        dests = [[getattr(Airport, c), 0] for c in watch.dest_codes]
        stops = MaxStops.NON_STOP if watch.nonstop else MaxStops.ANY
        segments = [FlightSegment(departure_airport=origins, arrival_airport=dests,
                                  travel_date=start.isoformat())]
        extra: dict = {}
        if watch.stay_days:
            back = start + timedelta(days=watch.stay_days)
            segments.append(FlightSegment(departure_airport=dests, arrival_airport=origins,
                                          travel_date=back.isoformat()))
            extra = {"trip_type": TripType.ROUND_TRIP, "duration": watch.stay_days}
        filters = DateSearchFilters(
            passenger_info=PassengerInfo(adults=1),
            flight_segments=segments,
            from_date=start.isoformat(),
            to_date=end.isoformat(),
            stops=stops,
            seat_type=SeatType.ECONOMY,
            **extra,
        )
        # Sold-out days / flights come back with price None.
        grid = [d for d in SearchDates().search(filters, **self._locale()) or [] if d.price]
        if not grid:
            return None
        best = min(grid, key=lambda d: d.price)
        dates = best.date if isinstance(best.date, tuple) else (best.date,)
        offer = Offer(
            price=float(best.price),
            currency=getattr(best, "currency", None) or self.config.currency,
            travel_date=dates[0].date().isoformat(),
            return_date=dates[1].date().isoformat() if len(dates) > 1 else None,
        )
        if not watch.stay_days:
            self._add_flight_details(watch, offer)
        return offer

    def _add_flight_details(self, watch: Watch, offer: Offer) -> None:
        """Best effort: the grid gives only a price, this names the actual flight."""
        from fli.models import (Airport, FlightSearchFilters, FlightSegment, MaxStops,
                                PassengerInfo, SeatType, SortBy)
        from fli.search import SearchFlights

        try:
            filters = FlightSearchFilters(
                passenger_info=PassengerInfo(adults=1),
                flight_segments=[FlightSegment(
                    departure_airport=[[getattr(Airport, c), 0] for c in watch.origin_codes],
                    arrival_airport=[[getattr(Airport, c), 0] for c in watch.dest_codes],
                    travel_date=offer.travel_date,
                )],
                seat_type=SeatType.ECONOMY,
                stops=MaxStops.NON_STOP if watch.nonstop else MaxStops.ANY,
                sort_by=SortBy.CHEAPEST,
            )
            flights = [f for f in SearchFlights().search(filters, **self._locale()) or []
                       if not isinstance(f, tuple) and f.price]
        except Exception as exc:  # details are optional; keep the grid price
            log.warning("flight details lookup failed for watch %s: %s", watch.id, exc)
            return
        if not flights:
            return
        top = min(flights, key=lambda f: f.price)
        first, last = top.legs[0], top.legs[-1]
        offer.airline = getattr(first.airline, "value", str(first.airline))
        offer.flight_no = f"{getattr(first.airline, 'name', '')}{first.flight_number}"
        offer.depart_time = first.departure_datetime.strftime("%H:%M")
        offer.stops = top.stops
        offer.origin = getattr(first.departure_airport, "name", None)
        offer.destination = getattr(last.arrival_airport, "name", None)
        # The grid can lag the live price slightly; the concrete flight wins.
        offer.price = float(top.price)


def google_flights_url(watch: Watch, offer: Offer, config: Config) -> str:
    origin = offer.origin or watch.origin_codes[0]
    dest = offer.destination or watch.dest_codes[0]
    q = f"Flights to {dest} from {origin} on {offer.travel_date}"
    q += f" through {offer.return_date}" if offer.return_date else " oneway"
    from urllib.parse import quote

    return (f"https://www.google.com/travel/flights?q={quote(q)}"
            f"&curr={config.currency}&hl={config.language}&gl={config.country}")
