# Digital Pathology Web Portal Demo

This repository contains a front-end prototype of a digital pathology review portal developed as part of my personal portfolio. The project explores how a pathology case review interface could integrate with a Digital Pathology Image Management System (IMS) to support slide viewing, and case navigation.
<a href="https://jaeseung-lee-engineer.github.io/prototype.html" target="_blank">
Open the Digital Pathology Demo Portal
</a>

## Project Overview
Digital pathology workflows typically rely on an Image Management System (IMS) to store, retrieve, and manage whole-slide images (WSI) along with associated case metadata.  

This prototype demonstrates a concept interface designed to interact with such systems by presenting slide images, case information, linked slides, and analysis outputs within a unified review environment.

<img src="images/digital_pathology_workflow.png" width="850">
Note: The FastAPI backend shown in this diagram is a conceptual component intended to illustrate how the portal could interact with a digital pathology image management system.

## Interface Concept

The layout reflects a typical pathology case review workflow:

- **Slide Viewer**  
  Displays whole-slide images retrieved from an external Image Management System.

- **Case Details Panel**  
  Shows metadata associated with the pathology case.

- **Linked Slides Panel**  
  Provides quick access to additional slides related to the same case.

- **Review Notes Panel**  
  Allows documentation of observations during slide review.

- **Annotation Toggle**  
  Displays AI- or pathologist-generated annotation highlighting regions of interest.

## IMS Integration Concept

The interface was designed with the assumption that slide images and metadata would be retrieved from an external Digital Pathology Image Management System through APIs.

In a full implementation, the viewer could interact with an IMS to retrieve:

- Whole-slide images (WSI)
- Case metadata
- Linked slide relationships
- Annotation layers

This prototype demonstrates how these elements could be organized in a streamlined review interface.

## Tech Stack

- HTML
- CSS
- JavaScript

## Disclaimer

- This project is a non-clinical demonstration prototype.
- It is intended for interface and workflow visualization only.
- Annotation displayed in the interface are illustrative and not intended for medical interpretation.
- Slide images referenced in the demo are from publicly available datasets where noted.

