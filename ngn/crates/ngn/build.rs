use std::{
    collections::BTreeMap,
    env, fs,
    io::{self, Write},
    path::{Path, PathBuf},
    process::Command,
};

fn main() {
    let web_ui = Path::new("../../../src/nagents/web-ui");
    let out_dir = PathBuf::from(env::var_os("OUT_DIR").expect("Cargo must set OUT_DIR"));
    for input in [
        "index.native.html",
        "vite.native.config.ts",
        "package.json",
        "package-lock.json",
        "tsconfig.json",
        "src",
        "public",
    ] {
        println!("cargo:rerun-if-changed={}", web_ui.join(input).display());
    }

    let vite = "node_modules/vite/bin/vite.js";
    assert!(
        web_ui.join(vite).is_file(),
        "Vite is missing. Run `npm ci --prefix src/nagents/web-ui` from the repository root before building ngn."
    );
    let dist = out_dir.join("web-ui");
    let status = Command::new("node")
        .current_dir(web_ui)
        .args([
            vite,
            "build",
            "--config",
            "vite.native.config.ts",
            "--outDir",
        ])
        .arg(&dist)
        .env("NODE_ENV", "production")
        .status()
        .unwrap_or_else(|error| panic!("Could not run Node to build the ngn frontend: {error}"));
    assert!(status.success(), "ngn frontend build failed: {status}");

    write_asset_table(&dist, &out_dir.join("web_assets.rs"))
        .expect("Could not embed the built ngn frontend");
}

fn asset_content_type(path: &str) -> Option<&'static str> {
    let bundle = path.starts_with("assets/native-");
    let image = bundle || path == "assets/ngn.svg" || path.starts_with("assets/provider-logos/");
    match path.rsplit_once('.')?.1 {
        "js" if bundle => Some("application/javascript; charset=utf-8"),
        "css" if bundle => Some("text/css; charset=utf-8"),
        "svg" if image => Some("image/svg+xml"),
        "png" if image => Some("image/png"),
        "ico" if image => Some("image/vnd.microsoft.icon"),
        _ => None,
    }
}

fn collect_assets(
    dist: &Path,
    dir: &str,
    assets: &mut BTreeMap<String, &'static str>,
) -> io::Result<()> {
    for entry in fs::read_dir(dist.join(dir))? {
        let entry = entry?;
        let Ok(name) = entry.file_name().into_string() else {
            continue;
        };
        if !name
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"._-".contains(&byte))
        {
            continue;
        }
        let path = format!("{dir}/{name}");
        let kind = entry.file_type()?;
        if kind.is_dir() && path == "assets/provider-logos" {
            collect_assets(dist, &path, assets)?;
        } else if kind.is_file()
            && let Some(content_type) = asset_content_type(&path)
        {
            assets.insert(path, content_type);
        }
    }
    Ok(())
}

fn write_asset_table(dist: &Path, output: &Path) -> io::Result<()> {
    let mut assets = BTreeMap::new();
    collect_assets(dist, "assets", &mut assets)?;
    let mut table = fs::File::create(output)?;
    // Only explicitly matched URLs exist at runtime; the paths in include_bytes!
    // are relative to this generated file and never come from an HTTP request.
    writeln!(
        table,
        "fn embedded_asset(path: &str) -> Option<(&'static str, &'static [u8])> {{
    match path {{
        \"/\" => Some((\"text/html; charset=utf-8\", include_bytes!(\"web-ui/index.native.html\"))),"
    )?;
    for (path, content_type) in assets {
        writeln!(
            table,
            "        \"/{path}\" => Some(({content_type:?}, include_bytes!(\"web-ui/{path}\"))),"
        )?;
    }
    writeln!(table, "        _ => None,\n    }}\n}}")
}
