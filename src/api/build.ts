/**
 * Static API builder.
 *
 * Everything a client could want is pre-rendered into files so the whole thing
 * can be served by GitHub Pages / any CDN with no server and no rate limits.
 */

import { readdir } from 'node:fs/promises';
import path from 'node:path';
import { loadConfig, PATHS, publicUrl, type Settings } from '../core/config.js';
import {
  copyDir,
  ensureDir,
  exists,
  formatBytes,
  readJsonOr,
  remove,
  writeJson,
  writeText,
} from '../core/fs.js';
import { Logger } from '../core/logger.js';
import { slugify } from '../core/text.js';
import type {
  ApiManifest,
  EnrichedChannel,
  EnrichedStream,
  HealthRecord,
} from '../core/types.js';
import { loadDataset, type Dataset } from '../aggregate/index.js';
import { loadHealthDb } from '../aggregate/index.js';
import { attachHealth } from '../health/index.js';
import { summarise, uptimeRatio } from '../health/scoring.js';
import { buildPlaylists } from './playlists.js';
import { buildSearchIndex } from './search-index.js';
import { buildOpenApi } from './openapi.js';
import { buildPlaylistCatalogue, buildReadmePlaylistBlock } from './catalogue.js';
import type { EpgLink } from '../epg/index.js';

const log = new Logger('api');

/** Public shape of a stream in the API — health flattened for easy consumption. */
interface PublicStream {
  channel: string | null;
  feed: string | null;
  title: string;
  url: string;
  referrer: string | null;
  user_agent: string | null;
  quality: string | null;
  rank: number;
  sources: string[];
  health: {
    status: string;
    score: number;
    uptime: number;
    checked_at: string;
    last_online: string | null;
    latency_ms: number | null;
    media: HealthRecord['media'];
  } | null;
}

interface PublicChannel {
  id: string;
  name: string;
  alt_names: string[];
  network: string | null;
  owners: string[];
  country: string;
  subdivision: string | null;
  categories: string[];
  languages: string[];
  broadcast_area: string[];
  timezones: string[];
  is_nsfw: boolean;
  launched: string | null;
  closed: string | null;
  replaced_by: string | null;
  website: string | null;
  logo: string | null;
  score: number;
  online: boolean;
  stream_count: number;
  best_quality: string | null;
  streams: PublicStream[];
  guides: EnrichedChannel['guides'];
}

function toPublicStream(stream: EnrichedStream): PublicStream {
  return {
    channel: stream.channel,
    feed: stream.feed,
    title: stream.title,
    url: stream.url,
    referrer: stream.referrer,
    user_agent: stream.user_agent,
    quality: stream.health?.media?.resolution ?? stream.quality,
    rank: stream.rank,
    sources: [...new Set(stream.provenance.map((entry) => entry.source))],
    health: stream.health
      ? {
          status: stream.health.status,
          score: stream.health.score,
          uptime: uptimeRatio(stream.health),
          checked_at: stream.health.checked_at,
          last_online: stream.health.last_online,
          latency_ms: stream.health.latency_ms,
          media: stream.health.media,
        }
      : null,
  };
}

function bestQuality(streams: readonly EnrichedStream[]): string | null {
  let best: string | null = null;
  let bestValue = 0;
  for (const stream of streams) {
    const quality = stream.health?.media?.resolution ?? stream.quality;
    const value = quality ? Number.parseInt(quality, 10) || 0 : 0;
    if (value > bestValue) {
      bestValue = value;
      best = quality;
    }
  }
  return best;
}

function toPublicChannel(channel: EnrichedChannel, includeOffline: boolean): PublicChannel {
  const streams = includeOffline
    ? channel.streams
    : channel.streams.filter((stream) => stream.health?.status !== 'offline');

  return {
    id: channel.id,
    name: channel.name,
    alt_names: channel.alt_names,
    network: channel.network,
    owners: channel.owners,
    country: channel.country,
    subdivision: channel.subdivision ?? null,
    categories: channel.categories,
    languages: channel.languages,
    broadcast_area: channel.broadcast_area,
    timezones: channel.timezones,
    is_nsfw: channel.is_nsfw,
    launched: channel.launched,
    closed: channel.closed,
    replaced_by: channel.replaced_by,
    website: channel.website,
    logo: channel.logo,
    score: channel.score,
    online: channel.online,
    stream_count: streams.length,
    best_quality: bestQuality(streams),
    streams: streams.map(toPublicStream),
    guides: channel.guides,
  };
}

export interface ApiBuildResult {
  generated_at: string;
  files: number;
  bytes: number;
  manifest: ApiManifest;
}

/** Writes the whole static site + API into `public/`. */
export async function buildApi(options: { clean?: boolean } = {}): Promise<ApiBuildResult> {
  const config = await loadConfig();
  const { settings } = config;

  const dataset = await loadDataset();
  if (!dataset) throw new Error('No dataset found. Run `aggregate` before `api`.');

  // Re-attach the newest health data — the scan may have run after aggregation.
  const healthDb = await loadHealthDb();
  attachHealth(dataset.channels, healthDb);

  if (options.clean) {
    // Keep public/epg: the `epg` step writes the merged guide there and this
    // function does not rebuild it. Wiping all of public/ is what made the
    // published guide 404 on every run.
    if (await exists(PATHS.public)) {
      for (const entry of await readdir(PATHS.public)) {
        const target = path.join(PATHS.public, entry);
        if (target === PATHS.epg) continue;
        await remove(target);
      }
    }
  }
  await ensureDir(PATHS.api);

  let files = 0;
  let bytes = 0;

  /**
   * Writes one API document.
   *
   * A `.gz` sibling is only produced for documents large enough to benefit.
   * With ~40k per-channel files, gzipping every one of them would double the
   * file count for a few hundred bytes saved each — and GitHub Pages already
   * compresses small text responses on the fly.
   */
  const GZIP_THRESHOLD = 32 * 1024;
  const emit = async (
    relativePath: string,
    data: unknown,
    pretty = settings.api.pretty,
  ): Promise<void> => {
    const target = path.join(PATHS.api, relativePath);
    await ensureDir(path.dirname(target));
    const serialized = pretty ? `${JSON.stringify(data, null, 2)}\n` : JSON.stringify(data);
    const size = Buffer.byteLength(serialized);
    bytes += await writeJson(target, data, {
      pretty,
      gzip: settings.api.gzip && size >= GZIP_THRESHOLD,
    });
    files++;
  };

  // ---- attach EPG links --------------------------------------------------
  const epgLinks = await readJsonOr<EpgLink[]>(path.join(PATHS.epgDb, 'links.json'), []);
  const linksByChannel = new Map<string, EnrichedChannel['guides']>();
  for (const link of epgLinks) {
    const bucket = linksByChannel.get(link.channel) ?? [];
    bucket.push({
      site: link.site,
      site_id: link.site_id,
      lang: '',
      confidence: link.confidence,
      method: link.method,
    });
    linksByChannel.set(link.channel, bucket);
  }
  for (const channel of dataset.channels) {
    const generated = linksByChannel.get(channel.id);
    if (generated) {
      const seen = new Set(channel.guides.map((guide) => `${guide.site}::${guide.site_id}`));
      for (const link of generated) {
        const key = `${link.site}::${link.site_id}`;
        if (!seen.has(key)) {
          channel.guides.push(link);
          seen.add(key);
        }
      }
    }
  }

  // ---- collections --------------------------------------------------------
  const publicChannels = dataset.channels.map((channel) =>
    toPublicChannel(channel, settings.api.include_offline),
  );
  const playable = publicChannels.filter((channel) => channel.stream_count > 0);

  await emit('channels.json', publicChannels);
  await emit('channels.online.json', playable.filter((channel) => channel.online));

  const allStreams = publicChannels.flatMap((channel) => channel.streams);
  await emit('streams.json', allStreams);
  await emit('orphans.json', dataset.orphans.map(toPublicStream));

  // Counts make these collections directly useful for building UI filters.
  /**
   * `channels` counts everything in the database, `playable` counts the ones
   * that actually have a stream. The gap is large — roughly a quarter of the
   * catalogue is watchable — so a UI that only shows `channels` promises far
   * more than it can deliver.
   */
  const countChannels = <T>(
    items: readonly T[],
    keyOf: (item: T) => string,
    matches: (channel: PublicChannel, key: string) => boolean,
  ): Array<T & { channels: number; playable: number; online: number }> =>
    items.map((item) => {
      const key = keyOf(item);
      const bucket = publicChannels.filter((channel) => matches(channel, key));
      return {
        ...item,
        channels: bucket.length,
        playable: bucket.filter((channel) => channel.stream_count > 0).length,
        online: bucket.filter((channel) => channel.online).length,
      };
    });

  const countries = countChannels(
    dataset.countries,
    (country) => country.code,
    (channel, code) => channel.country?.toUpperCase() === code.toUpperCase(),
  );
  const languages = countChannels(
    dataset.languages,
    (language) => language.code,
    (channel, code) => channel.languages.includes(code),
  );
  const categories = countChannels(
    dataset.categories,
    (category) => category.id,
    (channel, id) => channel.categories.includes(id),
  );

  await emit('countries.json', countries, true);
  await emit('languages.json', languages, true);
  await emit('categories.json', categories, true);
  await emit('regions.json', dataset.regions, true);
  await emit('subdivisions.json', dataset.subdivisions, true);
  await emit('timezones.json', dataset.timezones, true);
  await emit('guides.json', dataset.guides);

  // ---- health -------------------------------------------------------------
  const healthRecords = [...healthDb.values()];
  const summary = summarise(
    healthRecords,
    {
      alpha: settings.health.score_alpha,
      retireAfterFailures: settings.health.retire_after_failures,
      historySize: settings.health.history_size,
    },
    settings.health.healthy_threshold,
  );
  await emit(
    'health.json',
    {
      generated_at: dataset.generated_at,
      ...summary,
      by_country: Object.fromEntries(
        countries
          .filter((country) => country.channels > 0)
          .map((country) => [
            country.code,
            { channels: country.channels, playable: country.playable, online: country.online },
          ]),
      ),
    },
    true,
  );

  // ---- search index -------------------------------------------------------
  await emit('search.json', buildSearchIndex(publicChannels));

  // ---- shards -------------------------------------------------------------
  if (settings.api.shards) {
    const shardWrite = async (
      directory: string,
      buckets: Map<string, PublicChannel[]>,
    ): Promise<void> => {
      for (const [key, bucket] of buckets) {
        await emit(`by-${directory}/${slugify(key) || key}.json`, bucket);
      }
    };

    const group = (keyOf: (channel: PublicChannel) => string[]): Map<string, PublicChannel[]> => {
      const buckets = new Map<string, PublicChannel[]>();
      for (const channel of publicChannels) {
        for (const key of keyOf(channel)) {
          if (!key) continue;
          const bucket = buckets.get(key.toLowerCase()) ?? [];
          bucket.push(channel);
          buckets.set(key.toLowerCase(), bucket);
        }
      }
      return buckets;
    };

    await shardWrite('country', group((channel) => [channel.country ?? '']));
    await shardWrite('category', group((channel) => channel.categories));
    await shardWrite('language', group((channel) => channel.languages));
  }

  // ---- per-channel documents ---------------------------------------------
  if (settings.api.channel_details) {
    const detailed =
      settings.api.channel_details === 'all' ? publicChannels : playable;
    log.info(
      `Writing ${detailed.length} channel document(s) (mode: ${settings.api.channel_details})`,
    );
    for (const channel of detailed) {
      await emit(`channels/${encodeURIComponent(channel.id)}.json`, channel);
    }
  }

  // ---- playlists ----------------------------------------------------------
  const playlistResult = await buildPlaylists(dataset.channels, settings, {
    countries: new Map(dataset.countries.map((country) => [country.code, country.name])),
    categories: new Map(dataset.categories.map((category) => [category.id, category.name])),
    languages: new Map(dataset.languages.map((language) => [language.code, language.name])),
  });
  await emit('playlists.json', playlistResult.files, true);

  // ---- manifest -----------------------------------------------------------
  const epgReport = await readJsonOr<{ programmes?: number }>(
    path.join(PATHS.epgDb, 'report.json'),
    {},
  );

  const manifest: ApiManifest = {
    name: settings.project.name,
    version: '1.0.0',
    generated_at: dataset.generated_at,
    base_url: settings.project.base_url,
    counts: {
      channels: publicChannels.length,
      /** Channels with at least one stream — what a viewer can actually open. */
      playable_channels: playable.length,
      online_channels: publicChannels.filter((channel) => channel.online).length,
      feeds: dataset.channels.reduce((sum, channel) => sum + channel.feeds.length, 0),
      streams: allStreams.length,
      online_streams: summary.online,
      guides: dataset.guides.length,
      epg_programmes: epgReport.programmes ?? 0,
      countries: countries.filter((country) => country.channels > 0).length,
      languages: languages.filter((language) => language.channels > 0).length,
      categories: categories.filter((category) => category.channels > 0).length,
    },
    endpoints: {
      manifest: publicUrl(settings, 'api/v1/index.json'),
      channels: publicUrl(settings, 'api/v1/channels.json'),
      channels_online: publicUrl(settings, 'api/v1/channels.online.json'),
      streams: publicUrl(settings, 'api/v1/streams.json'),
      countries: publicUrl(settings, 'api/v1/countries.json'),
      languages: publicUrl(settings, 'api/v1/languages.json'),
      categories: publicUrl(settings, 'api/v1/categories.json'),
      regions: publicUrl(settings, 'api/v1/regions.json'),
      guides: publicUrl(settings, 'api/v1/guides.json'),
      health: publicUrl(settings, 'api/v1/health.json'),
      search: publicUrl(settings, 'api/v1/search.json'),
      channel_detail: publicUrl(settings, 'api/v1/channels/{id}.json'),
      by_country: publicUrl(settings, 'api/v1/by-country/{code}.json'),
      by_category: publicUrl(settings, 'api/v1/by-category/{id}.json'),
      by_language: publicUrl(settings, 'api/v1/by-language/{code}.json'),
      playlist: publicUrl(settings, 'playlists/index.m3u'),
      playlist_best: publicUrl(settings, 'playlists/best.m3u'),
      epg: publicUrl(settings, 'epg/guide.xml.gz'),
      openapi: publicUrl(settings, 'api/v1/openapi.json'),
    },
    upstream: dataset.upstream,
  };

  await emit('index.json', manifest, true);
  await emit('openapi.json', buildOpenApi(settings, dataset.generated_at), true);

  // ---- repository-facing catalogue ---------------------------------------
  // PLAYLISTS.md lives at the repo root rather than in public/ so it renders
  // on GitHub, which is where people actually go looking for a playlist URL.
  const catalogue = buildPlaylistCatalogue({
    settings,
    generatedAt: dataset.generated_at,
    files: playlistResult.files,
    counts: {
      channels: manifest.counts.channels,
      playable_channels: manifest.counts.playable_channels,
      streams: manifest.counts.streams,
      online_streams: manifest.counts.online_streams,
      epg_programmes: manifest.counts.epg_programmes,
    },
    countryFlags: new Map(dataset.countries.map((country) => [country.name, country.flag])),
  });
  await writeText(path.join(PATHS.root, 'PLAYLISTS.md'), catalogue);

  // Keep the README table in step with the generated catalogue.
  const readmePath = path.join(PATHS.root, 'README.md');
  if (await exists(readmePath)) {
    const { readText } = await import('../core/fs.js');
    const readme = await readText(readmePath);
    const block = buildReadmePlaylistBlock(settings);
    const marked = /<!-- PLAYLISTS:START -->[\s\S]*?<!-- PLAYLISTS:END -->/;
    if (marked.test(readme)) {
      const updated = readme.replace(marked, block);
      if (updated !== readme) await writeText(readmePath, updated);
    }
  }
  log.info('Wrote PLAYLISTS.md');

  // ---- static site --------------------------------------------------------
  if (await exists(PATHS.site)) {
    await copyDir(PATHS.site, PATHS.public);
    // GitHub Pages would otherwise run Jekyll and drop `_`-prefixed paths.
    await writeText(path.join(PATHS.public, '.nojekyll'), '');
    log.info('Copied static site into public/');
  }

  log.success(
    `API built: ${files} JSON files (${formatBytes(bytes)}), ` +
      `${playlistResult.files.length} playlists, ${publicChannels.length} channels`,
  );

  return { generated_at: dataset.generated_at, files, bytes, manifest };
}

export type { Dataset };
export * from './playlists.js';
export * from './search-index.js';
export * from './openapi.js';
export * from './catalogue.js';
