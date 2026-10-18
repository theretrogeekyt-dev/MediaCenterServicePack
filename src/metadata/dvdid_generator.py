import hashlib
import io
import logging
from pathlib import Path
from typing import Any, Dict, Optional
from lxml import etree
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)


def generate_dvd_id(title: str, year: Optional[str] = None) -> str:
    """Generate a deterministic WMC-compatible DVDId hash."""
    seed = f"{title}_{year or ''}".lower().encode("utf-8")
    h = hashlib.md5(seed).hexdigest()
    # Format as 16-hex standard DVD disc ID
    return f"openmce_{h[:16]}"


def create_fallback_poster(title: str, year: Optional[str] = None) -> bytes:
    """Generate an elegant fallback movie poster when no online artwork exists."""
    width, height = 600, 900
    img = Image.new("RGB", (width, height), color=(26, 32, 44))
    draw = ImageDraw.Draw(img)

    # Gradient border
    draw.rectangle([10, 10, width - 10, height - 10], outline=(74, 85, 104), width=3)

    # Draw title and year text
    display_text = f"{title}\n({year})" if year else title
    draw.text((width / 2, height / 2 - 20), display_text, fill=(247, 250, 252), anchor="mm")

    output = io.BytesIO()
    img.save(output, format="JPEG", quality=90)
    return output.getvalue()


class DVDIDGenerator:
    """Generates legacy Windows Media Center dvdid.xml sidecar files."""

    def build_xml(self, movie_data: Dict[str, Any]) -> bytes:
        """Construct the 5.0 MDR-DVD XML representation."""
        title = movie_data.get("title", "Unknown Title")
        release_date = movie_data.get("release_date", "")
        year = release_date[:4] if len(release_date) >= 4 else ""
        dvd_id = movie_data.get("dvd_id") or generate_dvd_id(title, year)

        root = etree.Element(
            "METADATA",
            nsmap={
                "xsi": "http://www.w3.org/2001/XMLSchema-instance",
                "xsd": "http://www.w3.org/2001/XMLSchema",
            },
        )

        mdr = etree.SubElement(root, "MDR-DVD")
        etree.SubElement(mdr, "version").text = "5.0"
        etree.SubElement(mdr, "dvdTitle").text = title
        etree.SubElement(mdr, "title").text = title
        etree.SubElement(mdr, "studio").text = movie_data.get("studio", "Unknown Studio")

        # Cast
        performers = movie_data.get("lead_performers", [])
        etree.SubElement(mdr, "leadPerformer").text = ", ".join(performers) if performers else "Featured Cast"

        etree.SubElement(mdr, "director").text = movie_data.get("director", "Unknown Director")
        etree.SubElement(mdr, "MPAARating").text = movie_data.get("mpaa_rating", "NR")
        etree.SubElement(mdr, "releaseDate").text = release_date

        genres = movie_data.get("genres", [])
        etree.SubElement(mdr, "genre").text = ", ".join(genres) if genres else "General"

        etree.SubElement(mdr, "synopsis").text = movie_data.get("overview", "")
        etree.SubElement(mdr, "runtime").text = str(movie_data.get("runtime", 120))
        etree.SubElement(mdr, "dataProvider").text = "TheMovieDatabase"
        etree.SubElement(mdr, "largeCoverParams").text = "folder.jpg"
        etree.SubElement(mdr, "smallCoverParams").text = "folder.jpg"

        etree.SubElement(root, "NeedsAttribution").text = "false"
        etree.SubElement(root, "DvdId").text = dvd_id

        return etree.tostring(
            root,
            xml_declaration=True,
            encoding="utf-8",
            pretty_print=True,
        )

    def write_sidecars(
        self,
        target_dir: Path,
        movie_data: Dict[str, Any],
        image_bytes: Optional[bytes] = None,
    ) -> tuple[Path, Path]:
        """
        Write dvdid.xml, folder.jpg, and poster.jpg to the specified target directory.
        """
        target_dir.mkdir(parents=True, exist_ok=True)

        # 1. Write dvdid.xml
        xml_content = self.build_xml(movie_data)
        dvdid_file = target_dir / "dvdid.xml"
        dvdid_file.write_bytes(xml_content)
        logger.info(f"Wrote {dvdid_file}")

        # 2. Write folder.jpg & poster.jpg
        img_data = image_bytes or create_fallback_poster(
            movie_data.get("title", "Movie"),
            movie_data.get("release_date", "")[:4],
        )

        folder_jpg = target_dir / "folder.jpg"
        poster_jpg = target_dir / "poster.jpg"

        folder_jpg.write_bytes(img_data)
        poster_jpg.write_bytes(img_data)
        logger.info(f"Wrote artwork to {folder_jpg} and {poster_jpg}")

        return dvdid_file, folder_jpg

