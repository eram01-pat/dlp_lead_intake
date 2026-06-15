from src.matching.normalize import normalize, keyword_pattern


def test_lowercase():
    assert normalize("Fleet WASHING Services") == "fleet wash service"


def test_hyphen_to_space():
    assert "drive thru" in normalize("Drive-Thru Washing")


def test_canadian_centre():
    assert "center" in normalize("Recreation Centre Cleaning")


def test_stemming_washing():
    assert "wash" in normalize("Fleet Washing").split()


def test_stemming_cleaning():
    assert "clean" in normalize("Cleaning Services").split()


def test_degreasing_stem():
    assert "degrease" in normalize("Degreasing of Municipal Trucks").split()


def test_washington_boundary():
    """keyword_pattern('washing') must not match 'washington'"""
    pat = keyword_pattern("washing")
    assert not pat.search(normalize("Washington Ave Resurfacing"))


def test_washing_matches_washing():
    pat = keyword_pattern("washing")
    assert pat.search(normalize("Fleet Washing Services"))


def test_post_construction_hyphen():
    norm = normalize("Post-Construction Cleaning")
    assert "post" in norm
    assert "construction" in norm
    assert "clean" in norm
