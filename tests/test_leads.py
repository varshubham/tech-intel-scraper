from scraper.core.db import DB
from scraper.events.extract import soupify
from scraper.leads import contacts, places

PAGE = """<html><body>
<nav><a href="/contact-us">Contact Us</a> <a href="/about/management">Our Management</a>
<a href="https://facebook.com/x">FB</a> <a href="/services">Services</a>
<a href="http://www.cityhospital.in/contact-us/">Contact again</a>
<a href="/blog/stress-management-tips">Stress management</a></nav>
<p>Padma Shri</p><p>President of India</p>
<h2>Our Team</h2>
<div><h4>Dr. Anjali Mehra</h4><p>Medical Director</p></div>
<div><p>Rakesh Kumar, Purchase Manager</p></div>
<footer>Write to info@cityhospital.in or <a href="mailto:admin@cityhospital.in">us</a>.
Call +91 98765 43210 / <a href="tel:011-2345-6789">011-2345-6789</a> logo@2x.png</footer>
</body></html>"""


def test_contact_links_same_site_best_first():
    links = contacts.contact_links(soupify(PAGE), "https://cityhospital.in/")
    assert links == ["https://cityhospital.in/contact-us", "https://cityhospital.in/about/management"]


def test_emails_and_phones():
    emails, phones = contacts.emails_and_phones(soupify(PAGE))
    assert emails == ["admin@cityhospital.in", "info@cityhospital.in"]
    assert "+91 98765 43210" in phones and "011-2345-6789" in phones


def test_find_people():
    people = {p["name"]: p["designation"] for p in contacts.find_people(soupify(PAGE))}
    assert people == {"Dr. Anjali Mehra": "Medical Director", "Rakesh Kumar": "Purchase Manager"}


def test_skip_social_domains_only():
    assert contacts._is_skipped("www.facebook.com")
    assert not contacts._is_skipped("linux.com")


def test_parse_osm_and_distance():
    elements = [
        {"type": "node", "id": 1, "lat": 28.57, "lon": 77.21,
         "tags": {"amenity": "hospital", "name": "City Hospital", "phone": "+91 11 1234 5678",
                  "website": "cityhospital.in", "addr:street": "Ring Road", "addr:city": "Delhi"}},
        {"type": "way", "id": 2, "center": {"lat": 28.6, "lon": 77.2}, "tags": {"amenity": "clinic"}},  # no name
    ]
    [b] = places.parse_osm(elements, {("amenity", "hospital"): "hospital"})
    assert (b["place_id"], b["category"], b["address"]) == ("osm:node/1", "hospital", "Ring Road, Delhi")
    assert round(places.distance_km(28.6139, 77.2090, 28.5672, 77.2100), 1) == 5.2


def test_db_upsert_business(tmp_path):
    db = DB(tmp_path / "t.db")
    b = {"place_id": "osm:node/1", "name": "City Hospital", "category": "hospital", "distance_km": 1.2,
         "emails": ["a@b.in"], "people": [{"name": "Dr. A", "designation": "Director"}], "source": "openstreetmap"}
    db.upsert_business(b)
    db.upsert_business({**b, "emails": ["a@b.in", "c@b.in"]})
    [row] = db.list_businesses()
    assert row["emails"] == ["a@b.in", "c@b.in"] and row["people"][0]["name"] == "Dr. A"


def test_static_site(tmp_path):
    from scraper.site import write_site

    db = DB(tmp_path / "t.db")
    db.upsert_event({"title": "PyCon <India>", "start_date": "2099-09-01", "city": "Pune",
                     "registration_url": "https://in.pycon.org/", "source_url": "https://x"}, "test")
    index = write_site(db, tmp_path / "site")
    page = index.read_text()
    assert "PyCon &lt;India&gt;" in page and 'href="events.xlsx"' in page and "/api/runs" not in page
    assert (tmp_path / "site" / "events.xlsx").stat().st_size > 0
