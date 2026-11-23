// "How a build goes": the two choices rewrite steps 2, 4 and 5 and the terminal.
// Every command here is a real Graft flag; see the README's Quick start.
(function () {
  var form = document.querySelector(".build");
  if (!form) return;

  var source = {
    have: {
      text: "Drop an IPA you already have on the app's pane. Graft checks that its bundle id matches the app, and that every binary in it is decrypted. One that's still encrypted is decrypted on the way in, once decryption is set up.",
      cmd: "graft --provide-ipa ~/Downloads/Example.ipa --app example",
      note: ""
    },
    device: {
      text: "The device's Decrypt card dumps the app off your own jailbroken iPhone or iPad, with frida-ios-dump or bagbak over SSH, and Graft picks whichever can run. Those tools aren't in the app: you install them yourself.",
      cmd: "graft --decrypt com.example.app",
      note: "dumps the installed app"
    },
    store: {
      text: "Sign in and set up decryption once, in Settings \u203a App Store. Then Get latest from App Store, on the app's pane, downloads it with your Apple ID through ipatool, and UnfairPlay decrypts it on your Mac. A free app your Apple ID hasn't got yet shows Get (free) instead. Your Apple ID and licence are stripped from the copy.",
      cmd: "graft --appstore-get --app example",
      note: "download, decrypt, stage"
    }
  };
  var delivery = {
    livecontainer: {
      build: "Press Build & Install. cyan injects the tweaks. The IPA stays unsigned and keeps its original bundle id, because LiveContainer signs it when you import it. This works with a free Apple ID.",
      deliver: "Graft pushes it into LiveContainer's Documents over USB or Wi-Fi. Import it in LiveContainer and the tweaks load."
    },
    sideload: {
      build: "Press Build & Install. cyan injects the tweaks, and zsign signs the result with your certificate and provisioning profile. This needs a paid developer account.",
      deliver: "Graft installs it on your iPhone or iPad as a normal app, next to everything else on the home screen."
    }
  };

  var stepSource = form.querySelector('[data-step="source"]');
  var stepBuild = form.querySelector('[data-step="build"]');
  var stepDeliver = form.querySelector('[data-step="deliver"]');
  var code = form.querySelector("[data-commands]");
  var copy = form.querySelector("[data-copy]");
  var current = [];

  function esc(s) { return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }
  function line(cmd, note) {
    return '<span class="ln"><span class="p">$</span> ' + esc(cmd) + (note ? '  <span class="c"># ' + esc(note) + "</span>" : "") + "</span>";
  }
  function value(name) { return form.querySelector('input[name="' + name + '"]:checked').value; }

  function render() {
    var s = source[value("source")], d = value("delivery");
    stepSource.textContent = s.text;
    stepBuild.textContent = delivery[d].build;
    stepDeliver.textContent = delivery[d].deliver;
    var rows = [
      ["graft --add-app https://apps.apple.com/us/app/example/id123456789 --delivery " + d, ""],
      [s.cmd, s.note],
      ["graft --fetch com.example.sometweak", "from canister.me"],
      ["graft --attach-tweak some-tweak.deb --app example", ""],
      ["graft example", "build and deliver"]
    ];
    current = rows.map(function (r) { return r[0]; });
    code.innerHTML = rows.map(function (r) { return line(r[0], r[1]); }).join("");
  }

  form.addEventListener("change", function (e) {
    if (e.target.matches('input[type="radio"]')) render();
  });

  copy.addEventListener("click", function () {
    var text = current.join("\n");
    var done = function () {
      copy.textContent = "Copied";
      setTimeout(function () { copy.textContent = "Copy commands"; }, 1600);
    };
    if (navigator.clipboard) navigator.clipboard.writeText(text).then(done, function () {});
  });

  render();
})();
