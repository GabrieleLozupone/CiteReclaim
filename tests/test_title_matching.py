from citation_reconciler.matching import (
    author_overlap,
    author_surname,
    first_author_matches,
    normalize_title,
    title_in_text,
    title_similarity,
)


def test_normalize_title_unicode_and_punctuation():
    assert normalize_title("Alzheimer’s  Disease: A <i>Study</i>!") == "alzheimer s disease a study"
    assert normalize_title("Café—Über Résumé") == "cafe uber resume"
    assert normalize_title(None) == ""


def test_title_similarity_exact_and_fuzzy():
    a = "Latent Diffusion Autoencoders: Toward Efficient Representation Learning"
    assert title_similarity(a, a.upper()) == 100
    assert title_similarity(a, a.replace("Toward", "Towards")) >= 96
    assert title_similarity(a, "A completely different paper about graphs") < 60
    assert title_similarity(a, None) == 0


def test_title_in_reference_text():
    ref = (
        "Lozupone, G. et al.: Latent diffusion autoencoders: toward efficient and meaningful "
        "unsupervised representation learning in medical imaging (2025). arXiv:2504.08635"
    )
    title = (
        "Latent Diffusion Autoencoders: Toward Efficient and Meaningful Unsupervised "
        "Representation Learning in Medical Imaging"
    )
    assert title_in_text(title, ref) == 100
    assert title_in_text("Short", ref) == 0  # short titles never partial-match


def test_author_surnames():
    assert author_surname("Lozupone, G.") == "lozupone"
    assert author_surname("G. Lozupone") == "lozupone"
    assert author_surname("Gabriele Lozupone") == "lozupone"
    assert author_overlap(["G. Lozupone", "A. Bria"], ["Gabriele Lozupone", "X Y"]) == 0.5
    assert first_author_matches(["Lozupone, G."], ["Gabriele Lozupone"])
    assert not first_author_matches([], ["Gabriele Lozupone"])
