"""Lecture de l'en-tete des fichiers PLY."""

from __future__ import annotations

import struct

from app.pipeline import ply

ENTETE_ASCII = (
    "ply\n"
    "format ascii 1.0\n"
    "comment cree par un test\n"
    "element vertex 3\n"
    "property float x\nproperty float y\nproperty float z\n"
    "element face 1\n"
    "property list uchar int vertex_indices\n"
    "end_header\n"
)


def test_compte_les_elements(tmp_path):
    chemin = tmp_path / "modele.ply"
    chemin.write_text(ENTETE_ASCII + "0 0 0\n1 0 0\n0 1 0\n3 0 1 2\n")

    assert ply.elements(chemin) == {"vertex": 3, "face": 1}
    assert "3" in ply.resume(chemin) and "sommets" in ply.resume(chemin)
    assert ply.est_vide(chemin) is False


def test_entete_ascii_dans_un_fichier_binaire(tmp_path):
    """L'en-tete reste en ASCII meme quand les donnees sont binaires."""
    chemin = tmp_path / "binaire.ply"
    entete = (
        "ply\nformat binary_little_endian 1.0\n"
        "element vertex 2\n"
        "property float x\nproperty float y\nproperty float z\n"
        "end_header\n"
    ).encode("ascii")
    chemin.write_bytes(entete + struct.pack("<6f", 1, 2, 3, 4, 5, 6))

    assert ply.elements(chemin) == {"vertex": 2}
    assert ply.est_vide(chemin) is False


def test_fichier_sans_sommet(tmp_path):
    """Cas reel : la commande reussit et n'ecrit qu'un en-tete."""
    chemin = tmp_path / "vide.ply"
    chemin.write_text(
        "ply\nformat ascii 1.0\nelement vertex 0\n"
        "property float x\nproperty float y\nproperty float z\nend_header\n"
    )

    assert ply.elements(chemin) == {"vertex": 0}
    assert ply.est_vide(chemin) is True
    assert "0 sommets" in ply.resume(chemin)


def test_fichier_qui_n_est_pas_un_ply(tmp_path):
    chemin = tmp_path / "autre.ply"
    chemin.write_text("ceci n'est pas un PLY\n")

    assert ply.elements(chemin) == {}
    assert ply.resume(chemin) == "format non reconnu"
    # Sans en-tete lisible, on ne declare pas le fichier vide : on ne sait pas.
    assert ply.est_vide(chemin) is False


def test_fichier_absent(tmp_path):
    assert ply.elements(tmp_path / "rien.ply") == {}
    assert ply.est_vide(tmp_path / "rien.ply") is False


def test_entete_interminable_ne_bloque_pas(tmp_path):
    """Un fichier sans end_header ne doit pas faire lire tout le disque."""
    chemin = tmp_path / "sans_fin.ply"
    chemin.write_text("ply\n" + "comment blabla\n" * 5000)

    assert ply.elements(chemin) == {}
