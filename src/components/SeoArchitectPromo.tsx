import { openUrl } from '@tauri-apps/plugin-opener';

import bannerUrl from '../assets/seo-content-architect.webp';

/**
 * Cross-promotion for the other Diflowrin desktop app, in the sidebar.
 *
 * The sidebar is the only placement. A full banner at the foot of Settings was
 * built and then removed on 2026-09-06: two placements for one product is one
 * more than the product needs, and this one is permanent and visible from every
 * tab, where the Settings banner was seen only by someone who went looking.
 *
 * The artwork is a crop. The supplied file is a 259x933 skyscraper, which at
 * this column's width would render 749px tall and push the backend footer off
 * the bottom of the sidebar - so `scripts/` keeps no copy and the asset here is
 * the top block alone: logo, headline, tagline, cut at the row where the
 * illustration starts. The image itself carries the product name, which is why
 * nothing below it repeats one.
 *
 * The links are constants so a changed Store link or a campaign `cid` is a
 * one-line edit.
 */

const STORE_URL = 'https://apps.microsoft.com/detail/9nl3gzlph01z?cid=DevShareMCLPCS';
const PAGE_URL = 'https://diflowrin.com/seo-content-architect/';

export function SeoArchitectSidebarLink() {
  return (
    <div className="sidebar-promo">
      {/* The artwork ends in a "Get Started Now" call to action in the original,
          which the crop drops - a picture of a button is not one. The whole
          image is the button instead, and it goes where that one pointed. */}
      <button
        type="button"
        className="sidebar-promo__art"
        onClick={() => void openUrl(STORE_URL)}
      >
        <img
          src={bannerUrl}
          alt="SEO Content Architect — AI-powered content creation. Opens the Microsoft Store."
        />
      </button>
      <div className="sidebar-promo__meta">
        {/* Says whose product this is. Clip Architect asks for nothing, and an
            unattributed advert inside it would read as one of its own screens. */}
        <span className="sidebar-promo__from">Also from Diflowrin</span>
        <button
          type="button"
          className="sidebar-promo__link"
          onClick={() => void openUrl(PAGE_URL)}
        >
          What it does
        </button>
      </div>
    </div>
  );
}
