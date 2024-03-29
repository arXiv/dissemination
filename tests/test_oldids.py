
def test_oldids_in_ps_cache(client):
    # resp = client.get("/pdf/acc-phys/9502001v1.pdf")
    # assert "9502001v1" in resp.text

    # resp = client.get("/pdf/cs/0011004v1.pdf")
    # assert "0011004v1" in resp.text

    # resp = client.get("/pdf/cs/0011004v2.pdf")
    # assert "0011004v2" in resp.text

    resp = client.get("/pdf/cs/0011004.pdf")
    assert "0011004v2" in resp.text

    resp = client.get("/pdf/cs/0011004.pdf")
    assert "0011004v2" in resp.text


def test_oldids_pdf_only(client):
    resp = client.get("/pdf/cs/0212040v1.pdf")
    assert "0212040v1" in resp.text

    resp = client.get("/pdf/cs/0212040.pdf")
    assert "0212040v1" in resp.text


def test_pdf_only_v1_and_2_tex_v3(client):
    resp = client.get("/pdf/cs/0012007v1.pdf")
    assert "0012007v1" in resp.text

    resp = client.get("/pdf/cs/0012007v2.pdf")
    assert "0012007v2" in resp.text

    resp = client.get("/pdf/cs/0012007v3.pdf")
    assert "0012007v3" in resp.text

    resp = client.get("/pdf/cs/0012007.pdf")
    assert "0012007v3" in resp.text
