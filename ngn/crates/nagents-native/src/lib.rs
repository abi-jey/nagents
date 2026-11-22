//! In-process native helpers. The engine supplies policy and I/O authority.

pub mod diff;
pub mod discovery;

#[cfg(all(feature = "python", unix))]
mod workspace;

#[cfg(feature = "python")]
mod bindings {
    use pyo3::{exceptions::PyValueError, prelude::*};

    const MAX_TEXT: usize = 4 * 1024 * 1024;
    const MAX_OUTPUT: usize = 1024 * 1024;

    /// Render the same preview as the Rust host from already-authorized bytes.
    /// `path` is only a label. This helper neither opens a file nor applies a change.
    #[pyfunction]
    #[pyo3(signature = (path, old, new, max_output))]
    fn diff_preview(
        py: Python<'_>,
        path: String,
        old: Option<String>,
        new: String,
        max_output: usize,
    ) -> PyResult<String> {
        if path.is_empty()
            || path.len() > 4096
            || path.chars().any(char::is_control)
            || new.len() > MAX_TEXT
            || old.as_ref().is_some_and(|text| text.len() > MAX_TEXT)
            || !(1..=MAX_OUTPUT).contains(&max_output)
        {
            return Err(PyValueError::new_err("Invalid or oversized diff input"));
        }
        let preview = py.detach(move || super::diff::change(&path, old.as_deref(), &new));
        if preview.len() > max_output {
            return Err(PyValueError::new_err(
                "Diff exceeds approval preview limit; make a smaller edit",
            ));
        }
        Ok(preview)
    }

    #[pymodule]
    fn nagents_native(module: &Bound<'_, PyModule>) -> PyResult<()> {
        module.add_function(wrap_pyfunction!(diff_preview, module)?)?;
        #[cfg(unix)]
        module.add_function(wrap_pyfunction!(super::workspace::workspace_paths, module)?)?;
        #[cfg(unix)]
        module.add_function(wrap_pyfunction!(
            super::workspace::workspace_search,
            module
        )?)?;
        Ok(())
    }
}
