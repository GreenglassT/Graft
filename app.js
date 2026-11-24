// "How a build goes": the two choices rewrite steps 2, 4 and 5.
(function () {
  var form = document.querySelector(".build");
  if (!form) return;

  var source = {
    have: "Drop an IPA you already have on the app's pane. Graft checks that its bundle id matches the app, and that every binary in it is decrypted. One that's still encrypted is decrypted on the way in, once decryption is set up.",
    device: "The device's Decrypt card dumps the app off your own jailbroken iPhone or iPad, with frida-ios-dump or bagbak over SSH, and Graft picks whichever can run. Those tools aren't in the app: you install them yourself.",
    store: "Sign in and set up decryption once, in Settings › App Store. Then Get latest from App Store, on the app's pane, downloads it with your Apple ID through ipatool, and UnfairPlay decrypts it on your Mac. A free app your Apple ID doesn't have yet shows Get (free) instead. Your Apple ID and license are stripped from the copy."
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

  function value(name) { return form.querySelector('input[name="' + name + '"]:checked').value; }

  function render() {
    var d = delivery[value("delivery")];
    stepSource.textContent = source[value("source")];
    stepBuild.textContent = d.build;
    stepDeliver.textContent = d.deliver;
  }

  form.addEventListener("change", function (e) {
    if (e.target.matches('input[type="radio"]')) render();
  });

  render();
})();

// Star on GitHub: the repo's star count, from one anonymous request (no cookies, no
// referrer). If it fails, or GitHub says nothing usable, the button shows no number.
(function () {
  var counts = document.querySelectorAll("[data-stars]");
  if (!counts.length || !window.fetch) return;
  fetch("https://api.github.com/repos/GreenglassT/Graft", { credentials: "omit", referrerPolicy: "no-referrer" })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (repo) {
      var n = repo && repo.stargazers_count;
      if (typeof n !== "number" || !(n > 0)) return;
      var text = n < 1000 ? String(n) : (Math.floor(n / 100) / 10).toString().replace(/\.0$/, "") + "k";
      for (var i = 0; i < counts.length; i++) {
        counts[i].textContent = text;
        counts[i].setAttribute("aria-label", n === 1 ? "1 star" : n + " stars");
        counts[i].hidden = false;
      }
    })
    .catch(function () {});
})();
