// "How a build goes": the two choices rewrite steps 1, 3 and 4 and the terminal.
// Every command here is a real Graft flag; see the README's Quick start.
(function () {
  var form = document.querySelector(".build");
  if (!form) return;

  var source = {
    have: {
      text: "Bring a decrypted IPA you already have. Graft checks that its bundle id matches the app, and that every binary in it is decrypted.",
      cmd: "graft --provide-ipa ~/Downloads/Example.ipa --app example",
      note: ""
    },
    device: {
      text: "Dump it from the app installed on your own jailbroken iPhone or iPad, with frida-ios-dump or bagbak over SSH. Graft picks whichever can run.",
      cmd: "graft --decrypt com.example.app",
      note: "dumps the installed app"
    },
    store: {
      text: "Download it with your own Apple ID through ipatool, and decrypt it on your Mac with UnfairPlay. Your Apple ID and licence are stripped from the copy.",
      cmd: "graft --appstore-get --app example",
      note: "download, decrypt, stage"
    }
  };
  var delivery = {
    livecontainer: {
      build: "cyan injects the tweaks. The IPA stays unsigned and keeps its original bundle id, because LiveContainer signs it when you import it.",
      deliver: "Graft pushes it into LiveContainer's Documents over USB or Wi-Fi. Import it in LiveContainer and the tweaks load."
    },
    sideload: {
      build: "cyan injects the tweaks, and zsign signs the result with your certificate and provisioning profile.",
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
      ["graft --add-app https://apps.apple.com/app/id123456789 --delivery " + d, ""],
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
