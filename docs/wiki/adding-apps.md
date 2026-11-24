# Adding Apps

Add an app to Graft by searching the App Store, or from its App Store URL or a
name and bundle id.

1. Press **+** at the top of the sidebar. Add App opens on **Search**.
2. Type an app's name, and pick it from the results.
3. Choose its delivery, and press **Add**.

You can also paste its App Store URL, or fill in a name and bundle id by hand.

## Add App

Add App (**+** at the top of the sidebar) has three ways in.

### Search

**Search** is the default. Type an app's name, and the App Store's iPhone apps
that match appear as you type, each with its icon, developer and price.

![The Add App sheet on the Search tab, listing five example apps with their developers and prices.](img/add-app-search.png "The Add App sheet on Search, with results for \"example\".")

- Results come from the store in the sheet's Store picker. It starts at this
  Mac's region (System Settings › General › Language & Region). A territory
  such as Puerto Rico or Réunion uses the store its people shop in (US,
  France), and a region with no App Store uses the US store and says so.
- Pick one with the mouse or the arrow keys, choose its delivery, and press
  **Add** (or Return). That adds it exactly as its App Store URL would.
- A paid app shows its price, and a listing that gives none says "Price
  unknown", never "Free".
- A result already in your config says so, and names the app it was added as.
- Apple's own apps can be found but not got: iOS ships them, and only Apple
  can sign them.
- Apple limits how often one Mac can search. A term you already searched
  while the sheet is open is answered from memory, and if Apple does say no,
  the sheet says to try again shortly.

### From App Store URL

Paste the app's `apps.apple.com` link.

### Manual

A name and a bundle id, and optionally an IPA you already have.

## Which store an app is looked up in

Graft remembers the store an app came from (`store_country` in config.yaml),
and checks its updates, its icon and its price there. Edit App shows it under
App Store and can change it.
- An app added from a URL keeps the store the URL names
  (`apps.apple.com/gb/...`, or the older `itunes.apple.com/gb/...`).
- An app with no store (from a URL that names none, or added by hand) is
  looked up in the store of this Mac's region, then in the US store. The
  first that sells it answers, and nothing is written. When neither has it, the
  error names both stores.
- A hand-written `store_country` that isn't an App Store is ignored, with a
  warning; `UK` is read as `GB`.
