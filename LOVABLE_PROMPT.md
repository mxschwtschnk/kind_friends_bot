# Lovable Prompt: Kind Friends Web App

Build a full-stack web application called **"Kind Friends"** — a friend network and link-sharing platform. This is a 1:1 recreation of an existing Telegram bot as a modern web app. The logic, rules, and user flows must match exactly as described below.

---

## Tech Stack

- **Frontend**: React + TypeScript + Tailwind CSS + shadcn/ui
- **Backend**: Supabase (Auth, Database, Edge Functions, Realtime)
- **Routing**: React Router
- **State**: React Query (TanStack Query) for server state

---

## Core Concept

Kind Friends lets users build a small circle of friends (max 15) and share links with them. It includes anti-spam protections, a personal wishlist, pause/resume functionality, and a feedback system. Think of it as a private, intimate link-sharing network.

---

## Database Schema (Supabase/PostgreSQL)

### `profiles` (extends Supabase auth.users)
```sql
CREATE TABLE profiles (
  id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  username TEXT UNIQUE NOT NULL,
  display_name TEXT,
  avatar_url TEXT,
  is_paused BOOLEAN DEFAULT FALSE,
  sent_links_count BIGINT DEFAULT 0,
  invites_sent_count BIGINT DEFAULT 0,
  sent_links_today_count INT DEFAULT 0,
  sent_links_date DATE,
  is_admin BOOLEAN DEFAULT FALSE,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
```

### `friendships`
```sql
CREATE TABLE friendships (
  id SERIAL PRIMARY KEY,
  user_id UUID REFERENCES profiles(id) ON DELETE CASCADE,
  friend_id UUID REFERENCES profiles(id) ON DELETE CASCADE,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(user_id, friend_id)
);
```
Friendships are stored as **two rows** (bidirectional): if A befriends B, insert both (A→B) and (B→A).

### `pending_friend_requests`
```sql
CREATE TABLE pending_friend_requests (
  id SERIAL PRIMARY KEY,
  requester_id UUID REFERENCES profiles(id) ON DELETE CASCADE,
  recipient_id UUID REFERENCES profiles(id) ON DELETE CASCADE,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(requester_id, recipient_id)
);
```

### `pending_links` (stored for paused users)
```sql
CREATE TABLE pending_links (
  id SERIAL PRIMARY KEY,
  recipient_id UUID REFERENCES profiles(id) ON DELETE CASCADE,
  sender_id UUID REFERENCES profiles(id) ON DELETE CASCADE,
  url TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
```

### `sent_links_history` (cooldown tracking)
```sql
CREATE TABLE sent_links_history (
  sender_id UUID REFERENCES profiles(id) ON DELETE CASCADE,
  url TEXT NOT NULL,
  sent_at TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (sender_id, url)
);
```

### `wishlists`
```sql
CREATE TABLE wishlists (
  id SERIAL PRIMARY KEY,
  owner_id UUID REFERENCES profiles(id) ON DELETE CASCADE,
  url TEXT NOT NULL,
  title TEXT,
  shop TEXT,
  product_name TEXT,
  price TEXT,
  image_url TEXT,
  reserved_by_id UUID REFERENCES profiles(id),
  reserved_at TIMESTAMPTZ,
  got_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
```

### `notifications` (replaces Telegram push messages)
```sql
CREATE TABLE notifications (
  id SERIAL PRIMARY KEY,
  user_id UUID REFERENCES profiles(id) ON DELETE CASCADE,
  type TEXT NOT NULL, -- 'friend_request', 'friend_accepted', 'friend_declined', 'friend_removed', 'link_shared', 'friend_pool_full', 'system'
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  data JSONB, -- extra payload (request_id, sender_id, url, etc.)
  is_read BOOLEAN DEFAULT FALSE,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
```

---

## Authentication & Onboarding

- Use **Supabase Auth** (email + password, or magic link).
- After sign-up, prompt user to set a unique `@username` (alphanumeric + underscores, 3-20 chars).
- Store in `profiles` table.
- **Invite deep-links**: Support URL format `/invite/{user_id}`. When a new user signs up via this link, automatically create a mutual friendship with the inviter and notify both.

---

## Page Structure & Navigation

### Layout
- **Sidebar** (desktop) / **Bottom tab bar** (mobile) with 4 main sections:
  1. **Home** (link sharing area)
  2. **Friends** (friend management)
  3. **Wishlist** (personal wishlist)
  4. **Settings** (help, feedback, account)
- **Notification bell** in the header with unread count badge. Clicking opens a notification panel/drawer.

### Welcome Screen (after first login)
Display:
```
Hey 👋
I help to connect friends to support each other.

What you can do here:
• Create your circle of friends (up to 15).
• Share any link (your post, podcast, article, video or stream) and your friends instantly get it.
• Add any product's link to your personal Wishlist.
• Look up for your friends' Wishes.
• Anti-spam limits (5 links/day + 7-day cooldown per link).

Just send me a link — I handle the rest.

This is an MVP version. Your feedback is very welcome!
```

---

## Feature 1: Link Sharing (Home Page)

### UI
- Large text input area with placeholder: "Paste a link to share with friends or add to wishlist..."
- A **"Submit"** button.
- Below: a feed/history of links the user has shared today, with status indicators.

### Link Detection
- When user submits text, extract URLs using this regex logic:
  - Full URLs: `https?://...`
  - www. domains: `www.example.com/...`
  - Bare domains: `example.com/path`
- Auto-prepend `https://` if missing.
- Strip trailing punctuation: `).,!?;:"'}>]`
- Deduplicate URLs.

### Link Action Dialog
When URL(s) are detected, show a modal/action sheet:
- **Single link**: Two buttons — "Add to 🎁 Wishlist" | "Send to 👥 Friends"
- **Multiple links**: Only — "Add to 🎁 Wishlist" (cannot send multiple at once)
- Plus a "Cancel" button.

### Send to Friends Logic
Before sending, validate in order:
1. **7-day cooldown**: Check `sent_links_history` — if this exact URL was sent by this user within 7 days, show: *"You already shared this link recently. Please wait X day(s) before sending it again to avoid spam."*
2. **Pause check**: If user `is_paused`, show: *"You are currently on pause. Resume to send and receive links again."*
3. **Daily limit**: Check `sent_links_today_count` (reset if `sent_links_date ≠ today`). Max **5 links/day**. If exceeded: *"You have reached the daily limit of 5 links. 0/5 links are left for today."*

If all checks pass, for each friend:
- If friend `is_paused` → insert into `pending_links` (stored for later digest)
- If friend is active → create a `notification` of type `link_shared` with the URL

After sending:
- Increment `sent_links_count` and `sent_links_today_count`
- Upsert `sent_links_history` with current timestamp
- Show result: *"Got your link ✅ Sent to X active friend(s). Saved for Y friend(s) who are on pause. You have Z/5 link(s) left for today."*
- If no friends: *"Got your link ✅ Right now you don't have any friends to send it to."*

### Link Metadata Extraction (for Wishlist)
When adding a link to wishlist, fetch metadata via a Supabase Edge Function:
- Request the URL with a browser-like User-Agent header
- Parse the HTML (first 200KB) and extract:
  - **Product name**: from JSON-LD (`@type: Product`), then meta tags (`og:title`, `twitter:title`), then `<title>`, then `<h1>`, then URL slug as fallback. Clean title by removing store names and domain patterns.
  - **Shop**: extract domain from URL (strip `www.`)
  - **Price**: from JSON-LD offers, then meta tags (`product:price:amount`). Include currency if available.
  - **Image**: from JSON-LD, then meta tags (`og:image`), then largest `<img>` tag by dimensions.

---

## Feature 2: Friends Management

### Friends Page
Show:
- Header: **"You have X/15 friends"**
- Three tabs/sections: **My Friends** | **Invite** | **Remove**

### My Friends Tab
- List of friend cards showing `@username` and avatar
- Click a friend → **Friend Card Modal**:
  - "Remove" button
  - "🎁 View Wishlist" button

### Invite Tab
- Input field: "Enter your friend's @username"
- Below, show a **shareable invite link**: `https://[your-app-domain]/invite/{user_id}`
- Show invite message text that can be copied:
  ```
  👋 I use Kind Friends to share my links with friends so they can support me with likes and comments whenever I post something new, and so I can support them when they need it too.

  I'd love to add you! Open this link and press Start to join: [invite_link]
  ```

### Invite Logic (when user enters @username)
Validate in order:
1. Check if inviter's friend count ≥ 15 → *"You already have 15 friends. Unfortunately there is a limit because it's MVP..."*
2. Look up username in `profiles`:
   - **Not found**: Show invite link message (as above). Increment `invites_sent_count`.
   - **Found, is self**: *"You cannot add yourself."*
   - **Found, already friends**: *"You are already connected with @username."*
   - **Found, their pool is full** (≥15 friends): *"Unfortunately it's not possible to add this friend because their friend pool is full."*
   - **Found, outgoing request already exists**: *"You already sent an invitation. Please wait for your friend to respond."*
   - **Found, incoming request from them exists**: Auto-accept! Create mutual friendship, notify both, delete the pending request.
   - **Otherwise**: Create `pending_friend_request`, send notification to recipient.

### Friend Request Notification
Recipient sees in their notification panel:
```
@username wants to add you as a friend on Kind Friends.
Ready to confirm?
[✅ Accept] [❌ Decline]
```

### Accept Logic
- Check both users' friend counts haven't exceeded limits
- If requester's pool is full → delete request, notify both
- If recipient's pool is full → delete request, notify both
- Otherwise: create mutual friendship (2 rows), notify both, check & warn about pending requests exceeding limits

### Decline Logic
- Delete request
- Notify requester: *"@username declined your invitation. You can send another request later."*

### Remove Friend
- Confirmation dialog: *"Are you sure you want to remove @username?"*
- On confirm: Delete both friendship rows, notify removed friend: *"@username removed you from Kind Friends. You are no longer connected."*
- After removal, if user has pending outgoing requests AND reached friend limit, show: *"Your friend pool is full. These outstanding invitations can no longer be accepted..."*

---

## Feature 3: Wishlist

### Wishlist Page
Show three tabs: **My Wishes** | **Add** | **Delete**

### My Wishes Tab
- List all wishlist items where `got_at IS NULL`
- Each item card shows:
  - Product image (if available)
  - Product name or shortened URL (max 32 chars, with `…`)
  - Shop domain
  - Price (if available)
  - Status badge: "Reserved by me" / "Reserved by friend" / available
  - Link to the URL
- **Owner actions** per item:
  - If not reserved: [Delete] [Reserve]
  - If reserved by self: [Got it!] [Unreserve]
  - If reserved by someone else: [Got it!]

### Add Tab
- Input field: "Paste a link to add to your wishlist"
- On submit: extract URL, fetch metadata via Edge Function, save to `wishlists` table
- Show confirmation with extracted metadata

### Delete Tab
- Show all items with [Delete] buttons
- Confirmation required before deletion
- Cannot delete reserved items: *"You can't delete this wish because it is reserved."*

### Friend's Wishlist View (from Friend Card)
- Show friend's wishlist items, but with visibility rules:
  - Hide items where `got_at IS NOT NULL`
  - Hide items reserved by someone else (only show unreserved items + items reserved by the viewer)
- **Friend/Viewer actions**:
  - If unreserved: [Reserve]
  - If reserved by viewer: [Unreserve]

### Wishlist Actions
- **Reserve**: Set `reserved_by_id` and `reserved_at`. Fail if already reserved by someone else.
- **Unreserve**: Clear `reserved_by_id` only if it matches the current user.
- **Got it!**: Set `got_at` timestamp. Only the owner can do this. Item disappears from lists.
- **Delete**: Only owner, only if not reserved.

---

## Feature 4: Pause / Resume

### Pause
- Toggle in Settings or a button on the Home page
- Sets `is_paused = TRUE`
- While paused:
  - Cannot send links (show warning)
  - Cannot access Friends management (show: *"You are currently on pause. Resume to manage friends again."*)
  - Incoming links from friends are stored in `pending_links`
- **Paused UI**: Show only Resume, Wishlist, and Settings navigation. Hide Friends and Home link input.
- Display: *"You are now on pause. While you're on pause: • You can't send links • You won't receive new links from friends. Tap Resume when you want to come back."*

### Resume
- Sets `is_paused = FALSE`
- Fetch all `pending_links` for this user, grouped by date
- Show a **digest** page/modal:
  ```
  Here is what you missed while you were on pause:

  📅 2025-01-15
  @friend1 — https://example.com/link1
  @friend2 — https://example.com/link2

  📅 2025-01-16
  @friend3 — https://example.com/link3
  ```
- Clear all pending links after showing digest
- If no pending links: *"Welcome back! 👋 You are active again. You did not miss any links while you were on pause."*

---

## Feature 5: Notifications (Real-time)

Use **Supabase Realtime** to subscribe to the `notifications` table for the current user.

### Notification Types
| Type | Title | Body example |
|------|-------|-------------|
| `friend_request` | Friend Request | "@user wants to add you as a friend" |
| `friend_accepted` | Request Accepted | "@user accepted your invitation. You're now friends!" |
| `friend_declined` | Request Declined | "@user declined your invitation." |
| `friend_removed` | Friend Removed | "@user removed you from Kind Friends." |
| `link_shared` | Link Shared | "@user shared a link with you: [url]" |
| `friend_pool_full` | Friend Pool Full | "Your friend pool is full. Outstanding invitations can no longer be accepted." |
| `system` | System | Various system messages |

### Notification UI
- Bell icon with unread count badge in header
- Dropdown/drawer showing notifications newest-first
- Friend request notifications have inline Accept/Decline buttons
- Link shared notifications are clickable (open the URL)
- "Mark all as read" button
- Individual dismiss/read on click

---

## Feature 6: Settings Page

### Sections
1. **How To** — Help text (same as the How To text from the bot):
   ```
   1️⃣ Share links
   • Paste any link — choose to send or wishlist.
   • If "Send", active friends get it instantly; paused friends get it when they return.

   2️⃣ Wishlist
   • If "Wishlist", you save links for later in 🎁 Wishlist.
   • View, edit, or delete items anytime. Browse your friends' Wishlists.

   3️⃣ Anti-spam Limits
   • Up to 5 links/day.
   • Same link: 7-day cooldown.
   • If your friend list is full, we'll warn you about new incoming invites.

   4️⃣ Friends
   • Add friends via Friends → Invite.
   • Remove via Friends → Remove or from a friend's card.
   • Existing users get a request; new users get a shareable invite link.
   • Max 15 friends.

   5️⃣ Pause / Resume
   • Pause stops sending & receiving; links are saved for later.
   • Resume gives you a quick digest of what you missed.

   6️⃣ Wipe account
   • Deletes your data, friendships, requests, wishlist & stored links.
   ```

2. **Feedback** — Simple text form. On submit, store in a `feedback` table or send via Edge Function. Show: *"Thanks! I delivered your feedback to the admin."* Only accept text (no file uploads).

3. **Wipe Account** — Dangerous action with confirmation:
   - *"Are you sure you want to wipe your Kind Friends account? This will remove all friends, delete any stored pending links, and new links will no longer arrive. Continue?"*
   - [Yes, wipe] / [No, keep my account]
   - On confirm: cascade delete all user data (friendships, requests, pending links, wishlist items, sent link history, profile) and sign out.

4. **Pause/Resume toggle** — Quick access to pause/resume from Settings too.

---

## Business Rules Summary

| Rule | Value |
|------|-------|
| Max friends per user | 15 |
| Max daily links | 5 |
| Link cooldown (same URL) | 7 days |
| Friendship storage | Bidirectional (2 rows) |
| Wishlist visibility for friends | Hide got items, hide items reserved by others |
| Reserved items | Cannot be deleted by owner |
| Pause mode | No sending, no receiving, links queued |
| Daily counter reset | Automatic when date changes (UTC) |
| Account wipe | Cascade delete everything |

---

## UI/UX Guidelines

- **Clean, minimal design** with a warm, friendly feel. Use soft rounded cards and gentle colors.
- **Color palette**: Warm tones — soft coral/peach primary, with neutral grays. Think friendly and inviting.
- **Mobile-first** responsive design. Bottom tab bar on mobile, sidebar on desktop.
- **Toast notifications** for quick feedback (link sent, friend added, etc.)
- **Confirmation dialogs** for destructive actions (remove friend, delete wishlist item, wipe account).
- **Loading states** with skeleton placeholders.
- **Empty states** with helpful illustrations and CTAs (e.g., "No friends yet — invite someone!" with invite button).
- **Real-time updates** — friend requests, shared links, and wishlist changes should appear without refresh.

---

## Row Level Security (Supabase RLS)

Enable RLS on all tables:
- `profiles`: Users can read any profile, update only their own.
- `friendships`: Users can read/delete only rows where they are `user_id`. Insert requires being `user_id`.
- `pending_friend_requests`: Requester can insert/delete. Recipient can read/delete.
- `pending_links`: Sender can insert. Recipient can read/delete.
- `sent_links_history`: Owner only (CRUD).
- `wishlists`: Owner can CRUD. Friends can read (with visibility rules applied in queries). Friends can update `reserved_by_id` for reservation.
- `notifications`: User can only read/update their own notifications.

---

## Summary

This is a complete social link-sharing web app with:
1. **Auth + profiles** with unique usernames
2. **Friend network** with invite links, requests, accept/decline, removal (max 15)
3. **Link sharing** with anti-spam (5/day + 7-day cooldown per URL) and delivery to friends
4. **Wishlist** with metadata extraction, reserve/unreserve/got-it mechanics
5. **Pause/resume** with pending link digest on resume
6. **Real-time notifications** replacing Telegram push messages
7. **Feedback system** and **account wipe**
8. **Invite deep-links** for user acquisition

Build all pages, components, database tables, RLS policies, and Edge Functions needed for this to work end-to-end.
