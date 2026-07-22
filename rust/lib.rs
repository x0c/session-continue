use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use serde_json::Value;
use std::collections::hash_map::DefaultHasher;
use std::hash::{Hash, Hasher};
use unicode_width::UnicodeWidthChar;

fn value_to_python(py: Python<'_>, value: Value) -> PyResult<PyObject> {
    Ok(match value {
        Value::Null => py.None(),
        Value::Bool(value) => value.into_pyobject(py)?.to_owned().unbind().into(),
        Value::Number(value) => {
            if let Some(value) = value.as_i64() {
                value.into_pyobject(py)?.to_owned().unbind().into()
            } else if let Some(value) = value.as_u64() {
                value.into_pyobject(py)?.to_owned().unbind().into()
            } else {
                value
                    .as_f64()
                    .unwrap_or_default()
                    .into_pyobject(py)?
                    .to_owned()
                    .unbind()
                    .into()
            }
        }
        Value::String(value) => value.into_pyobject(py)?.unbind().into(),
        Value::Array(values) => {
            let list = PyList::empty(py);
            for value in values {
                list.append(value_to_python(py, value)?)?;
            }
            list.unbind().into()
        }
        Value::Object(values) => {
            let dict = PyDict::new(py);
            for (key, value) in values {
                dict.set_item(key, value_to_python(py, value)?)?;
            }
            dict.unbind().into()
        }
    })
}

#[pyfunction]
fn loads(py: Python<'_>, data: &[u8]) -> PyResult<PyObject> {
    let value: Value = py
        .allow_threads(|| serde_json::from_slice(data))
        .map_err(|error| PyValueError::new_err(format!("JSON 解析失败：{error}")))?;
    value_to_python(py, value)
}

#[derive(Clone, Copy, Debug, Default, Eq, Hash, PartialEq)]
enum Colour {
    #[default]
    Default,
    Indexed(u8),
    Rgb(u8, u8, u8),
}

#[derive(Clone, Copy, Debug, Default, Eq, Hash, PartialEq)]
struct StyleState {
    fg: Colour,
    bg: Colour,
    bold: bool,
    dim: bool,
    underline: bool,
    reverse: bool,
}

impl StyleState {
    fn apply(&mut self, params: &[i32]) {
        let mut i = 0;
        while i < params.len() {
            let p = params[i];
            match p {
                0 => *self = Self::default(),
                1 => self.bold = true,
                2 => self.dim = true,
                4 => self.underline = true,
                7 => self.reverse = true,
                22 => {
                    self.bold = false;
                    self.dim = false;
                }
                24 => self.underline = false,
                27 => self.reverse = false,
                39 => self.fg = Colour::Default,
                49 => self.bg = Colour::Default,
                30..=37 => self.fg = Colour::Indexed((p - 30) as u8),
                40..=47 => self.bg = Colour::Indexed((p - 40) as u8),
                90..=97 => self.fg = Colour::Indexed((p - 90 + 8) as u8),
                100..=107 => self.bg = Colour::Indexed((p - 100 + 8) as u8),
                38 | 48 => {
                    let mut colour = None;
                    if i + 2 < params.len() && params[i + 1] == 5 {
                        colour = Some(Colour::Indexed(params[i + 2].clamp(0, 255) as u8));
                        i += 2;
                    } else if i + 4 < params.len() && params[i + 1] == 2 {
                        colour = Some(Colour::Rgb(
                            params[i + 2].clamp(0, 255) as u8,
                            params[i + 3].clamp(0, 255) as u8,
                            params[i + 4].clamp(0, 255) as u8,
                        ));
                        i += 4;
                    } else {
                        i += 1;
                    }
                    if let Some(colour) = colour {
                        if p == 38 {
                            self.fg = colour;
                        } else {
                            self.bg = colour;
                        }
                    }
                }
                _ => {}
            }
            i += 1;
        }
    }
}

#[derive(Clone, Debug, Eq, Hash, PartialEq)]
struct Cell {
    text: String,
    style: StyleState,
    continuation: bool,
}

impl Default for Cell {
    fn default() -> Self {
        Self {
            text: " ".to_string(),
            style: StyleState::default(),
            continuation: false,
        }
    }
}

type ColourTuple = (i8, u8, u8, u8);
type SpanTuple = (
    usize,
    usize,
    ColourTuple,
    ColourTuple,
    bool,
    bool,
    bool,
    bool,
);
type RowTuple = (String, Vec<SpanTuple>, u64);

fn colour_tuple(colour: Colour) -> ColourTuple {
    match colour {
        Colour::Default => (-1, 0, 0, 0),
        Colour::Indexed(value) => (0, value, 0, 0),
        Colour::Rgb(r, g, b) => (1, r, g, b),
    }
}

fn parse_params(body: &str) -> Vec<i32> {
    if body.is_empty() {
        return vec![0];
    }
    let mut values = Vec::new();
    for part in body.split(';') {
        match if part.is_empty() {
            Ok(0)
        } else {
            part.parse::<i32>()
        } {
            Ok(value) => values.push(value),
            Err(_) => return Vec::new(),
        }
    }
    values
}

fn parse_line(line: &str, width: usize) -> Vec<Cell> {
    let mut row = vec![Cell::default(); width];
    let chars: Vec<char> = line.chars().collect();
    let mut state = StyleState::default();
    let mut x = 0usize;
    let mut i = 0usize;
    while i < chars.len() && x < width {
        if chars[i] == '\u{1b}' {
            if i + 1 < chars.len() && chars[i + 1] == '[' {
                let mut j = i + 2;
                while j < chars.len() && !(('@'..='~').contains(&chars[j])) {
                    j += 1;
                }
                if j >= chars.len() {
                    break;
                }
                if chars[j] == 'm' {
                    let body: String = chars[i + 2..j].iter().collect();
                    state.apply(&parse_params(&body));
                }
                i = j + 1;
                continue;
            }
            let mut j = i + 1;
            while j < chars.len() && (' '..='/').contains(&chars[j]) {
                j += 1;
            }
            if j < chars.len() && ('0'..='~').contains(&chars[j]) {
                j += 1;
            }
            i = j;
            continue;
        }
        let ch = chars[i];
        let char_width = UnicodeWidthChar::width(ch).unwrap_or(0);
        if char_width == 0 {
            if x > 0 && !row[x - 1].continuation {
                row[x - 1].text.push(ch);
            }
            i += 1;
            continue;
        }
        row[x] = Cell {
            text: ch.to_string(),
            style: state,
            continuation: false,
        };
        if char_width >= 2 {
            if x + 1 >= width {
                row[x] = Cell::default();
                x += 1;
            } else {
                row[x + 1] = Cell {
                    text: " ".to_string(),
                    style: state,
                    continuation: true,
                };
                x += 2;
            }
        } else {
            x += 1;
        }
        i += 1;
    }
    row
}

fn compile_row(row: &[Cell]) -> RowTuple {
    let mut text = String::new();
    let mut spans = Vec::new();
    let mut char_pos = 0usize;
    let mut span_start = 0usize;
    let mut current: Option<StyleState> = None;
    for cell in row {
        if cell.continuation {
            continue;
        }
        text.push_str(&cell.text);
        if current != Some(cell.style) {
            if let Some(style) = current {
                if char_pos > span_start {
                    spans.push((
                        span_start,
                        char_pos,
                        colour_tuple(style.fg),
                        colour_tuple(style.bg),
                        style.bold,
                        style.dim,
                        style.underline,
                        style.reverse,
                    ));
                }
            }
            span_start = char_pos;
            current = Some(cell.style);
        }
        char_pos += cell.text.chars().count();
    }
    if let Some(style) = current {
        if char_pos > span_start {
            spans.push((
                span_start,
                char_pos,
                colour_tuple(style.fg),
                colour_tuple(style.bg),
                style.bold,
                style.dim,
                style.underline,
                style.reverse,
            ));
        }
    }
    let mut hasher = DefaultHasher::new();
    row.hash(&mut hasher);
    (text, spans, hasher.finish())
}

#[pyfunction]
fn parse_ansi_rows(py: Python<'_>, text: &str, width: usize, height: usize) -> Vec<RowTuple> {
    py.allow_threads(|| {
        let mut rows: Vec<RowTuple> = text
            .split('\n')
            .take(height)
            .map(|line| compile_row(&parse_line(line, width)))
            .collect();
        let blank = compile_row(&vec![Cell::default(); width]);
        while rows.len() < height {
            rows.push(blank.clone());
        }
        rows
    })
}

#[pymodule]
fn _native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(loads, module)?)?;
    module.add_function(wrap_pyfunction!(parse_ansi_rows, module)?)?;
    module.add("ACCELERATOR_VERSION", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
