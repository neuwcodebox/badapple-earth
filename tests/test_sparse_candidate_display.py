from PIL import Image, ImageColor
import pytest

from badapple_earth.production.assets import displayed_candidates
from badapple_earth.production import layout


@pytest.mark.parametrize("places",[1,2,3,4])
@pytest.mark.parametrize("reranked",[False,True])
def test_many_views_of_few_places_keep_the_actual_winner_without_padding(places,reranked):
    pool = [{"tile_id":str(i%places),"view_id":str(i),"rank":i+1} for i in range(20)]
    scores = [i/20 for i in range(20)] if reranked else None
    shown = displayed_candidates(pool,pool[17],5,scores)
    assert pool[17] in shown
    assert len(shown) == len({r["tile_id"] for r in shown}) == places
    assert all(row in pool for row in shown)
    assert [r["rank"] for r in shown] == sorted(r["rank"] for r in shown)


def test_one_requested_place_does_not_add_a_second():
    pool = [{"tile_id":str(i),"view_id":str(i),"rank":i+1} for i in range(3)]
    assert displayed_candidates(pool,pool[2],1) == [pool[2]]


def test_single_candidate_layout_has_no_connections_to_empty_slots(tmp_path):
    path = tmp_path/"photo.png"
    Image.new("RGB",(32,24),"white").save(path)
    plan = {"selected_candidate":0,"source_image":str(path),"selected_image":str(path),
            "candidates":[{"path":str(path),"rank":1,"raw_mse":.1}],"caption":"Test photo",
            "time":0,"duration":1,"slot":0,"total":1}
    image = layout.frame(plan,{"font_regular":"C:/Windows/Fonts/segoeui.ttf", "font_bold":"C:/Windows/Fonts/segoeuib.ttf"})
    assert image.size == (1920,1080)
    assert image.getpixel((370,layout.THUMBS[4][1]+72)) == ImageColor.getrgb(layout.BG)
