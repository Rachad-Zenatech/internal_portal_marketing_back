"""Approved Google resources from the Marketing Data Reader brand reference."""
from dataclasses import dataclass

from models.marketing.google_reporting_model import GoogleReportingBrand


@dataclass(frozen=True)
class ReaderAccount:
    name: str
    property_id: str
    site: str
    ads_id: str | None = None
    manager_id: str | None = None


ACCOUNTS = {
    "daas": ReaderAccount("DaaS", "506592241", "https://www.droneasaservice.com/", "5895292344", "9778775470"),
    "zenadrone": ReaderAccount("ZenaDrone", "298882096", "https://www.zenadrone.com/"),
    "zenatech": ReaderAccount("ZenaTech", "381966443", "https://www.zenatech.com/"),
    "deskflex": ReaderAccount("DeskFlex", "244918617", "https://www.deskflex.com/", "4630047213"),
    "tillerstack": ReaderAccount("TillerStack", "262452460", "https://www.tillerstack.com/", "9076943558"),
    "workaware": ReaderAccount("WorkAware", "360972536", "https://www.workaware.com/", "8057328994"),
    "k9sky": ReaderAccount("K9Sky", "360927476", "https://www.k9sky.com/"),
    "paceplus": ReaderAccount("PacePlus", "282396091", "https://www.paceplus.com/"),
}


def reporting_brands() -> list[GoogleReportingBrand]:
    return [GoogleReportingBrand(code=code, name=a.name, has_ads=bool(a.ads_id)) for code, a in ACCOUNTS.items()]
