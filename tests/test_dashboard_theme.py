"""Dark palette and Persian typography do not change dashboard behavior."""
import re
import unittest
from unittest.mock import patch

import dashboard


def contrast(first, second):
    def luminance(color):
        rgb = [int(color[i:i+2], 16) / 255 for i in (1, 3, 5)]
        rgb = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in rgb]
        return sum(v * weight for v, weight in zip(rgb, (0.2126, 0.7152, 0.0722)))
    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


class DashboardThemeTests(unittest.TestCase):
    def test_dark_theme_is_default_even_without_browser_javascript(self):
        with patch.object(dashboard, 'load_json', side_effect=lambda path, default: default):
            page = dashboard.render()
        self.assertIn('name="color-scheme" content="dark"', page)
        self.assertIn('name="theme-color" content="#0b111c"', page)
        self.assertIn(':root{color-scheme:dark;', dashboard.PAGE_CSS)
        self.assertNotIn('color-scheme:light', dashboard.PAGE_CSS)
        self.assertNotIn('filter:invert', dashboard.PAGE_CSS)

    def test_primary_text_and_semantic_status_colors_have_readable_contrast(self):
        palette = dict(re.findall(r'--([\w-]+):(#[0-9a-f]{6})(?=[;}])', dashboard.PAGE_CSS))
        for foreground, background in (
            ('ink', 'bg'), ('ink', 'card'), ('muted', 'card'), ('muted', 'raised'),
            ('green', 'success-bg'), ('amber', 'warning-bg'), ('red', 'danger-bg'),
            ('blue', 'info-bg'), ('yellow', 'navy'), ('navy', 'yellow'),
        ):
            with self.subTest(foreground=foreground, background=background):
                self.assertGreaterEqual(contrast(palette[foreground], palette[background]), 4.5)

    def test_persian_faces_use_only_installed_iransans_and_cover_regular_and_bold(self):
        faces = re.findall(r'@font-face\{([^}]+)\}', dashboard.PAGE_CSS)
        self.assertEqual(len(faces), 2)
        self.assertNotIn('Nazanin', dashboard.PAGE_CSS)
        self.assertIn('local("IRANSansWeb")', faces[0])
        self.assertIn('local("IRANSansWeb-Bold")', faces[1])
        for face, weight in zip(faces, ('400', '700')):
            self.assertIn('font-family:"Dashboard Persian"', face)
            self.assertIn('local("IRANSans', face)
            self.assertIn(f'font-weight:{weight}', face)
            self.assertIn('font-display:swap', face)
            self.assertIn('unicode-range:U+0600-06FF', face)
            self.assertNotIn('url(', face)  # No unlicensed binary or CDN dependency.
        self.assertIn('--font-ui:"Dashboard Persian",Tahoma,Arial,sans-serif', dashboard.PAGE_CSS)
        self.assertRegex(dashboard.PAGE_CSS, r'body\{[^}]*font-family:var\(--font-ui\)')
        self.assertRegex(dashboard.PAGE_CSS, r'\.taxi-card h3\{[^}]*var\(--font-ui\)')
        self.assertRegex(dashboard.PAGE_CSS, r'\.event-list p\{[^}]*var\(--font-ui\)')

    def test_panels_and_secondary_surfaces_are_dark_without_white_flashes(self):
        self.assertNotRegex(dashboard.PAGE_CSS, r'background:#fff(?:;|\})')
        for selector in ('.overview-card', '.metric', '.taxi-card', '.history-panel', '.filters'):
            self.assertRegex(dashboard.PAGE_CSS, re.escape(selector) + r'\{[^}]*background:var\(--card\)')
        self.assertIn('button[aria-pressed=true]{background:var(--yellow);color:var(--navy)}', dashboard.PAGE_CSS)


if __name__ == '__main__':
    unittest.main()
